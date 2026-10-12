"""KPSS Radar: a bounded background scanner and a small Flask dashboard."""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, Response, jsonify, render_template, request
import database as db
import eligibility
import scraper
from deadlines import parse_deadline_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("KPSS_RADAR")
app = Flask(__name__)

SCAN_INTERVAL_MINUTES = max(60, int(os.environ.get("SCAN_INTERVAL_MINUTES", "480")))
DETAIL_CACHE_MINUTES = max(15, int(os.environ.get("DETAIL_CACHE_MINUTES", "480")))
MAX_DETAIL_REQUESTS = max(1, min(100, int(os.environ.get("MAX_DETAIL_REQUESTS", "60"))))
DETAIL_RULE_VERSION = 5
MANUAL_SCAN_COOLDOWN_SECONDS = 15 * 60
scan_lock = threading.Lock()
is_scanning = False
scheduler = None
last_manual_scan_at = 0.0


class VerificationDeferred(Exception):
    pass


def cache_age(detail):
    try:
        fetched = datetime.fromisoformat(detail.get("_fetched_at", ""))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def cache_fresh(detail):
    return bool(detail and detail.get("_rules_version") == DETAIL_RULE_VERSION and cache_age(detail) < DETAIL_CACHE_MINUTES * 60)


def is_profile_ineligible(job):
    return eligibility.prefilter(job.get("title", ""), job.get("summary", ""))


def get_verified_job_detail(job_id, budget=None):
    job = db.get_job_by_id(job_id)
    if not job:
        raise ValueError("İlan bulunamadı")
    cached = job.get("detail_cache") or {}
    from_cache = cache_fresh(cached)
    if from_cache:
        detail = dict(cached)
        if scraper.is_sbb_url(job["url"]) and detail.get("document_url"):
            detail["document_url"] = job["url"]
    else:
        if budget is not None:
            with budget["lock"]:
                if budget["remaining"] <= 0:
                    raise VerificationDeferred()
                budget["remaining"] -= 1
        detail = scraper.fetch_job_detail(job)
        detail["_fetched_at"] = db.now_iso()
    analysis = eligibility.assess(detail.get("full_text", ""), detail.get("structured_rows", []),
                                  detail.get("general_text"), job["title"])
    detail.update(analysis)
    detail["_rules_version"] = DETAIL_RULE_VERSION
    detail["deadline_info"] = parse_deadline_info(job.get("published_date", ""),
        summary=job.get("summary", "") + "\n" + detail.get("full_text", ""))
    positions = detail.get("eligible_positions", [])
    detail["eligible_quota"] = sum(row.get("quota") or 0 for row in positions) if positions and all(row.get("quota") is not None for row in positions) else None
    detail["candidate_advice"] = (
        "2026 KPSS P3, en fazla 78 taban puan ve diploma kontrolü geçen kadrolar aşağıda. Diğer özel şartları da kontrol et."
        if detail["is_suitable"] else detail["reason"]
    )
    normalized_text = eligibility.fold(detail.get("general_text") or detail.get("full_text", ""))
    detail["employment_type"] = "4/B sözleşmeli" if "4/b" in normalized_text else "KİT sözleşmeli" if "399" in normalized_text else "Kadrolu memur" if "657" in normalized_text else "İstihdam türünü resmî ilandan kontrol et"
    selected_text = " ".join(row.get("title", "") + " " + row.get("qualification", "") for row in positions)
    tags = scraper.classify_job(job["title"], job["institution"], full_text=selected_text)
    if not detail["is_suitable"]:
        tags.append("UYGUN_DEGIL" if detail["profile_verified"] else "DOGRULANMADI")
    db.save_detail(job_id, detail, tags)
    return detail, from_cache


def verify_scanned_job(item, budget):
    job_id, is_new = item
    try:
        detail, from_cache = get_verified_job_detail(job_id, budget)
        if not from_cache:
            time.sleep(0.25)
        return job_id, is_new, "eligible" if detail["is_suitable"] else "excluded"
    except VerificationDeferred:
        return job_id, is_new, "deferred"
    except Exception as error:
        logger.warning("İlan doğrulanamadı (id=%s): %s", job_id, error)
        # Preserve the last document for diagnostics; stale results are hidden by the API.
        job = db.get_job_by_id(job_id)
        detail = dict(job.get("detail_cache") or {})
        detail["_last_error"] = str(error)[:300]
        detail["_attempted_at"] = db.now_iso()
        db.save_detail(job_id, detail, ["DOGRULANMADI"])
        return job_id, is_new, "error"


def _scan_worker():
    global is_scanning
    started = db.now_iso()
    total, inserted = 0, 0
    reports = {}
    try:
        scraped, reports = scraper.run_all_scrapers()
        total = len(scraped)
        candidates = {}
        excluded = 0
        for job in scraped:
            if is_profile_ineligible(job) or parse_deadline_info(job.get("published_date", ""), summary=job.get("summary", ""))["badge_type"] == "expired":
                excluded += 1
                continue
            job["tags"] = ["DOGRULANMADI"]
            job_id, is_new = db.insert_job(job)
            candidates[job_id] = candidates.get(job_id, False) or is_new
        # Refresh known matches first, then unread/old documents; no province preference.
        def verification_priority(item):
            detail = db.get_job_by_id(item[0]).get("detail_cache") or {}
            return (not detail.get("is_suitable", False), -cache_age(detail))
        candidates = sorted(candidates.items(), key=verification_priority)
        budget = {"remaining": MAX_DETAIL_REQUESTS, "lock": threading.Lock()}
        outcomes = {"eligible": 0, "excluded": excluded, "deferred": 0, "error": 0}
        for item in candidates:
            job_id, is_new, outcome = verify_scanned_job(item, budget)
            outcomes[outcome] += 1
            if is_new and outcome == "eligible":
                inserted += 1
        expired_deleted = db.delete_expired_jobs()
        failures = sum(report["status"] == "error" for report in reports.values())
        partial = any(report["status"] == "partial" for report in reports.values())
        status = "ERROR" if failures == len(reports) else "PARTIAL" if failures or partial or outcomes["error"] or outcomes["deferred"] else "SUCCESS"
        reports["__verification__"] = dict(outcomes, expired_deleted=expired_deleted)
        db.log_scan(started, total, inserted, status, reports)
        logger.info("Tarama: %s ilan, %s uygun, %s ertelenen; %s", total, outcomes["eligible"], outcomes["deferred"], status)
        return {"status": status.lower(), "total_scraped": total, "new_jobs_count": inserted, "verification": reports["__verification__"], "sources": reports}
    except Exception as error:
        logger.exception("Tarama tamamlanamadı")
        db.log_scan(started, total, inserted, "ERROR", reports, str(error)[:500])
        return {"status": "error", "message": str(error)}
    finally:
        is_scanning = False
        scan_lock.release()


def perform_scan():
    global is_scanning
    if not scan_lock.acquire(blocking=False):
        return {"status": "busy"}
    is_scanning = True
    return _scan_worker()


def start_scan():
    global is_scanning
    if not scan_lock.acquire(blocking=False):
        return False
    is_scanning = True
    threading.Thread(target=_scan_worker, daemon=True, name="SourceScan").start()
    return True


def start_manual_scan():
    global is_scanning, last_manual_scan_at
    if not scan_lock.acquire(blocking=False):
        return {"status": "busy"}
    now = time.monotonic()
    remaining = MANUAL_SCAN_COOLDOWN_SECONDS - (now - last_manual_scan_at)
    if remaining > 0:
        scan_lock.release()
        return {"status": "cooldown", "retry_after_seconds": int(remaining)}
    last_manual_scan_at = now
    is_scanning = True
    threading.Thread(target=_scan_worker, daemon=True, name="ManualSourceScan").start()
    return {"status": "started"}


def visible_jobs():
    jobs, seen = [], {}
    for job in db.get_jobs():
        detail = job.pop("detail_cache", None) or {}
        if not cache_fresh(detail) or not detail.get("is_suitable") or not detail.get("profile_verified"):
            continue
        if any(tag in job["tags"] for tag in ("UYGUN_DEGIL", "DOGRULANMADI")):
            continue
        positions = detail.get("eligible_positions") or []
        if not positions or any(row.get("score_type") != 3 or not isinstance(row.get("minimum_score"), (int, float)) or row["minimum_score"] > 78 or not eligibility.accepts_degree(row.get("qualification", "")) or eligibility.exclusion(row.get("title", "") + " " + row.get("qualification", "")) for row in positions):
            continue
        if detail.get("required_kpss_years") and eligibility.PROFILE_KPSS_YEAR not in detail["required_kpss_years"]:
            continue
        stored_deadline = detail.get("deadline_info") or {}
        info = parse_deadline_info("Son başvuru: " + stored_deadline["deadline_iso"]) if stored_deadline.get("confidence") == "explicit" and stored_deadline.get("deadline_iso") else parse_deadline_info(
            job.get("published_date", ""), summary=job.get("summary", ""))
        if info["badge_type"] == "expired":
            continue
        identity = detail.get("document_hash") or job["signature"]
        if identity in seen:
            primary = seen[identity]
            if job["source"] not in primary["all_sources"]:
                primary["all_sources"].append(job["source"])
            continue
        job.update({"deadline_info": info, "eligible_positions": detail.get("eligible_positions", []),
                    "eligible_quota": detail.get("eligible_quota"), "extra_conditions": detail.get("extra_conditions", []),
                    "required_kpss_years": detail.get("required_kpss_years", []),
                    "all_sources": [job["source"]], "web_url": job["url"],
                    "pdf_url": f"/api/jobs/{job['id']}/pdf" if detail.get("document_url") else None})
        seen[identity] = job
        jobs.append(job)
    return jobs


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
@app.get("/ping")
def health():
    return jsonify(status="healthy", service="kpss-radar", is_scanning=is_scanning)


@app.get("/api/jobs")
def api_get_jobs():
    try:
        limit = min(400, max(1, int(request.args.get("limit", 400))))
    except ValueError:
        return jsonify(status="error", message="Geçersiz ilan sayısı"), 400
    jobs = visible_jobs()
    search, source, tag = request.args.get("search", "").strip(), request.args.get("source", ""), request.args.get("tag", "")
    if search:
        needle = eligibility.fold(search)
        jobs = [job for job in jobs if needle in eligibility.fold(f"{job['title']} {job['institution']} {job['summary']}")]
    if source and source.upper() != "ALL":
        jobs = [job for job in jobs if source in job["all_sources"]]
    if tag and tag.upper() != "ALL":
        jobs = [job for job in jobs if tag in job["tags"]]
    return jsonify(status="success", jobs=jobs[:limit], count=len(jobs[:limit]), categories=eligibility.TAG_LABELS,
                   profile={"kpss_type": 3, "score": 78, "year": int(eligibility.PROFILE_KPSS_YEAR)},
                   stats={"total_jobs": len(jobs), "last_scan": db.last_scan()})


@app.get("/api/jobs/<int:job_id>/detail")
def api_get_job_detail(job_id):
    if not db.get_job_by_id(job_id):
        return jsonify(status="error", message="İlan bulunamadı"), 404
    try:
        detail, from_cache = get_verified_job_detail(job_id)
        return jsonify(status="success", detail=detail, from_cache=from_cache)
    except Exception as error:
        logger.warning("Detay okunamadı (id=%s): %s", job_id, error)
        return jsonify(status="error", message="Kaynak belge şu anda okunamıyor. Resmî ilanı açabilirsin."), 502


@app.get("/api/jobs/<int:job_id>/pdf")
def api_view_job_pdf(job_id):
    job = db.get_job_by_id(job_id)
    if not job:
        return "İlan bulunamadı", 404
    detail = job.get("detail_cache") or {}
    document_url = detail.get("document_url")
    if not document_url:
        return "Bu ilan için doğrulanmış PDF bağlantısı bulunamadı", 404
    try:
        return Response(scraper.pdf_bytes(document_url), content_type="application/pdf",
                        headers={"Content-Disposition": "inline; filename=kamu_ilan.pdf", "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
    except Exception:
        return render_template("document_error.html", message="Kaynak geçerli bir PDF sunmadı.", source_url=job["url"]), 502


@app.get("/api/scan/status")
def api_scan_status():
    last = db.last_scan()
    if last:
        last["verification"] = last.get("sources", {}).pop("__verification__", {})
    return jsonify(status="success", is_scanning=is_scanning, last_scan=last)


@app.post("/api/scan")
def api_manual_scan():
    return jsonify(start_manual_scan())


def start_scheduler():
    global scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(timezone="Europe/Istanbul")
    scheduler.add_job(perform_scan, "interval", minutes=SCAN_INTERVAL_MINUTES, max_instances=1, coalesce=True, id="source_scan")
    scheduler.start()
    timer = threading.Timer(3, start_scan)
    timer.daemon = True
    timer.start()


db.init_db()
db.delete_expired_jobs()
if os.environ.get("RUN_SCHEDULER", "true").lower() == "true":
    start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
