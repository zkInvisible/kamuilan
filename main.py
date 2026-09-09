import os
import threading
import logging
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, redirect
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

import database as db
import scraper
import telegram_notifier

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("KPSS_APP")

app = Flask(__name__)

# Konfigürasyon
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "60"))
DAILY_NOTIFICATION_HOUR = int(os.environ.get("DAILY_NOTIFICATION_HOUR", "9"))
DAILY_NOTIFICATION_MINUTE = int(os.environ.get("DAILY_NOTIFICATION_MINUTE", "0"))
SEND_IMMEDIATE = os.environ.get("SEND_IMMEDIATE_NOTIFICATIONS", "false").lower() in ["true", "1", "yes"]

scan_lock = threading.Lock()
is_scanning = False

def perform_scan_and_notify(is_manual: bool = False) -> dict:
    """İlanları tarar, veritabanına kaydeder ve gerekirse bildirim gönderir."""
    global is_scanning
    if not scan_lock.acquire(blocking=False):
        logger.warning("Tarama işlemi zaten devam ediyor, yeni istek atlandı.")
        return {"status": "busy", "message": "Zaten aktif bir tarama devam ediyor."}

    is_scanning = True
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_jobs = []
    total_scraped = 0
    error_msg = ""

    try:
        logger.info("=== Otomatik İlan Taraması Başlatıldı ===")
        scraped_jobs, counts = scraper.run_all_scrapers()
        total_scraped = len(scraped_jobs)

        for job in scraped_jobs:
            job_id, is_new = db.insert_job(job)
            if is_new:
                job["id"] = job_id
                new_jobs.append(job)

        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.log_scan(
            started_at=started_at,
            finished_at=finished_at,
            total_scraped=total_scraped,
            new_inserted=len(new_jobs),
            status="SUCCESS",
            error_message=""
        )
        logger.info(f"Tarama tamamlandı. Toplam taranan: {total_scraped}, Yeni eklenen: {len(new_jobs)}")

        # Bildirim Mantığı: Yalnızca günde 1 kez (scheduled_daily_digest) toplu bülten gönderilir.
        # Anlık veya manuel buton taramalarında Telegram mesajı atılmaz.
        if new_jobs and SEND_IMMEDIATE:
            logger.info(f"{len(new_jobs)} yeni ilan için Telegram bildirimi gönderiliyor (SEND_IMMEDIATE aktif)...")
            success, sent_count = telegram_notifier.send_daily_digest(new_jobs)
            if success:
                db.mark_as_notified([j["id"] for j in new_jobs if j.get("id")])

        return {
            "status": "success",
            "total_scraped": total_scraped,
            "new_jobs_count": len(new_jobs),
            "sources": counts
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Tarama sırasında hata oluştu: {e}", exc_info=True)
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.log_scan(
            started_at=started_at,
            finished_at=finished_at,
            total_scraped=total_scraped,
            new_inserted=len(new_jobs),
            status="ERROR",
            error_message=error_msg
        )
        return {"status": "error", "message": error_msg}
    finally:
        is_scanning = False
        scan_lock.release()

def scheduled_daily_digest():
    """Her gün belirlenen saatte (günde 1 kez) henüz bildirilmemiş yeni ve süresi geçerli ilanları topluca Telegram'a atar."""
    logger.info("=== Günlük Toplu Telegram Bülteni Görevi Tetiklendi ===")
    unnotified_jobs = db.get_unnotified_jobs()
    if not unnotified_jobs:
        logger.info("Bildirilecek bekleyen yeni ilan bulunamadı.")
        return

    # Süresi geçmiş veya tecrübe şartı arayanları bültenden ele ve işaretle
    valid_jobs = []
    expired_ids = []
    for j in unnotified_jobs:
        date_raw = j.get("published_date", "") or j.get("summary", "")
        info = scraper.parse_deadline_info(date_raw, source=j.get("source", ""), summary=j.get("summary", ""))
        if info.get("badge_type") == "expired":
            if j.get("id"):
                expired_ids.append(j["id"])
            continue
        if j.get("requires_experience") == 1 or "TECRUBE_SARTI" in j.get("tags", []):
            if j.get("id"):
                expired_ids.append(j["id"])
            continue
        valid_jobs.append(j)

    if expired_ids:
        db.mark_as_notified(expired_ids)

    if not valid_jobs:
        logger.info("Aktif yeni ilan kalmadı, bülten gönderilmedi.")
        return

    logger.info(f"{len(valid_jobs)} aktif ilan için günlük toplu Telegram bülteni gönderiliyor...")
    success, count = telegram_notifier.send_daily_digest(valid_jobs)
    if success:
        db.mark_as_notified([j["id"] for j in valid_jobs if j.get("id")])
        logger.info(f"{count} adet ilan günlük toplu bülten ile bildirildi ve işaretlendi.")

# --- FLASK WEB ROTLARI ---

@app.route("/")
def index():
    """Ana sayfa dashboard arayüzü."""
    return render_template("index.html")

@app.route("/health")
@app.route("/ping")
def health():
    """Render.com ve UptimeRobot için lightweight 7/24 sağlık kontrolü."""
    return jsonify({
        "status": "healthy",
        "service": "kpss-radar",
        "timestamp": datetime.now().isoformat(),
        "is_scanning": is_scanning
    })

@app.route("/api/jobs")
def api_get_jobs():
    """Filtreli, sıralı ve arama destekli ilan listesi (Süresi dolmuşlar elenir)."""
    tag = request.args.get("tag", "ALL")
    search = request.args.get("search", "")
    source = request.args.get("source", "ALL")
    sort_by = request.args.get("sort_by", "newest")
    only_favs = request.args.get("only_favorites", "false").lower() in ["true", "1", "yes"]
    include_expired = request.args.get("include_expired", "false").lower() in ["true", "1", "yes"]
    include_experience = request.args.get("include_experience", "false").lower() in ["true", "1", "yes"]
    limit = int(request.args.get("limit", 400))
    offset = int(request.args.get("offset", 0))

    jobs = db.get_jobs(
        tag=tag,
        search=search,
        source=source,
        only_favorites=only_favs,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        exclude_experience=not include_experience
    )
    
    # Her ilana son başvuru geri sayım bilgisini ekle, süresi dolmuşları ve tecrübe şartlıları ayıkla
    active_jobs = []
    for j in jobs:
        # Tecrübe şartı kontrolü (Yeni mezun için kesin eleme)
        if not include_experience and (j.get("requires_experience") == 1 or "TECRUBE_SARTI" in j.get("tags", [])):
            continue

        # Adaya uygun olmayan ilanları ana listeden ele
        if tag.upper() != "UYGUN_DEGIL" and "UYGUN_DEGIL" in j.get("tags", []):
            continue

        date_raw = j.get("published_date", "") or j.get("summary", "")
        info = scraper.parse_deadline_info(date_raw, source=j.get("source", ""), summary=j.get("summary", ""))
        j["deadline_info"] = info

        # Kullanıcı talebi: Süresi dolmuş ilanlar ana ekranda gösterilmez
        if not include_expired and info.get("badge_type") == "expired":
            continue
        active_jobs.append(j)

    stats = db.get_stats()

    return jsonify({
        "status": "success",
        "jobs": active_jobs,
        "count": len(active_jobs),
        "stats": stats
    })

@app.route("/api/jobs/favorite", methods=["POST"])
def api_toggle_favorite():
    """İlanın favori durumunu değiştirir."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id") or request.form.get("job_id")
    if not job_id:
        return jsonify({"status": "error", "message": "job_id belirtilmedi"}), 400

    try:
        new_state = db.toggle_favorite(int(job_id))
        stats = db.get_stats()
        return jsonify({
            "status": "success",
            "is_favorite": new_state,
            "favorites_count": stats["favorites_count"]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/jobs/<int:job_id>/detail")
def api_get_job_detail(job_id: int):
    """
    Belirli bir ilanın detaylı analizini, alınan pozisyonları, kontenjan sayısını,
    istihdam türünü, varsa resmi kadro tablosu görselini ve başvuru şartlarını döner.
    Performans için veritabanında önbelleğe (detail_cache) alır.
    """
    job = db.get_job_by_id(job_id)
    if not job:
        return jsonify({"status": "error", "message": "İlan bulunamadı"}), 404

    # 1. Önce veritabanı önbelleğine bak
    cached = db.get_job_detail_cache(job_id)
    if cached:
        # Önbellekteki metin ve kadroları güncel kural setiyle aday uygunluğuna göre anında doğrula
        paragraphs = cached.get("paragraphs", [])
        cadres = cached.get("cadres", [])
        content_txt = cached.get("content", "") or cached.get("full_text", "")
        full_text = " ".join(paragraphs) + " " + " ".join(cadres) + " " + content_txt
        
        is_suit, reason_code, reason_desc = scraper.evaluate_candidate_suitability(
            title=job["title"],
            text=full_text,
            institution=job.get("institution", ""),
            source=job.get("source", "")
        )
        if not is_suit:
            cached["is_suitable"] = False
            cached["candidate_advice"] = f"❌ ADAYA UYGUN DEĞİL: {reason_desc}"
            cached["advice_badge"] = "target_incompatible"
            db.update_job_tags(job_id, ["UYGUN_DEGIL"])
            db.update_job_detail_cache(job_id, cached)
        elif "MAKINE_MUHENDIS" in job.get("tags", []) and cached.get("advice_badge") in ["target_general", "target_engineer"]:
            cached["candidate_advice"] = "⚙️ DİPLOMA BİREBİR EŞLEŞMESİ: İlanda doğrudan Makine Mühendisliği / Mekatronik lisans diploması aranmaktadır. Tecrübe şartı aranmamaktadır; 80 puanınız güçlü bir puandır."
            cached["advice_badge"] = "target_match"
            db.update_job_detail_cache(job_id, cached)
        return jsonify({"status": "success", "detail": cached, "from_cache": True})

    # 2. Önbellekte yoksa detayları çek ve analiz et
    try:
        detail_data = scraper.fetch_and_analyze_job_detail(
            url=job["url"],
            source=job["source"],
            title=job["title"],
            institution=job["institution"],
            tags=job.get("tags", [])
        )
        # Aday uygunluk durumunu ve etiketlerini veritabanında senkronize et
        paragraphs = detail_data.get("paragraphs", [])
        cadres = detail_data.get("cadres", [])
        full_text = " ".join(paragraphs) + " " + " ".join(cadres)
        
        new_tags = scraper.classify_job(
            title=job["title"],
            institution=job["institution"],
            summary=job.get("summary", ""),
            full_text=full_text,
            source=job.get("source", "")
        )
        
        is_exp = bool(detail_data.get("requires_experience")) or ("TECRUBE_SARTI" in new_tags)
        exp_note = detail_data.get("experience_note", "")
        if is_exp and "TECRUBE_SARTI" not in new_tags:
            new_tags.append("TECRUBE_SARTI")
            
        if not detail_data.get("is_suitable", True) and "UYGUN_DEGIL" not in new_tags:
            new_tags.append("UYGUN_DEGIL")
            
        db.update_job_tags(job_id, new_tags)
        db.update_job_experience_status(job_id, is_exp, exp_note)

        # Önbelleğe kaydet
        db.update_job_detail_cache(job_id, detail_data)
        return jsonify({"status": "success", "detail": detail_data, "from_cache": False})
    except Exception as e:
        logger.error(f"Detay çekme hatası (job_id={job_id}): {e}")
        # Hata durumunda temel analizle fallback dön
        is_exp_fb, exp_note_fb = scraper.check_experience_requirement(f"{job['title']} {job.get('summary', '')}")
        fallback_detail = {
            "title": job["title"],
            "institution": job["institution"],
            "source": job["source"],
            "url": job["url"],
            "quota": "Kontenjan Belirtilmedi",
            "cadres": ["📋 İlanda Belirtilen Pozisyonlar"],
            "employment_type": "Kamu Personeli Alımı",
            "evaluation_method": "KPSS Puan Sıralaması",
            "kpss_requirement": "KPSS P3 (Lisans)",
            "candidate_advice": "Tecrübe şartı aranmamaktadır (Yeni mezun başvurabilir)." if not is_exp_fb else f"❌ Tecrübe şartı tespit edildi ({exp_note_fb}).",
            "table_images": [],
            "paragraphs": [job.get("summary", "")],
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "is_fresh_grad": not is_exp_fb,
            "requires_experience": 1 if is_exp_fb else 0,
            "experience_note": exp_note_fb if is_exp_fb else "Tecrübe şartı aranmamaktadır (Yeni mezunlar başvurabilir, staj yeterlidir)."
        }
        return jsonify({"status": "success", "detail": fallback_detail, "from_cache": False})

@app.route("/api/jobs/<int:job_id>/pdf")
def api_view_job_pdf(job_id: int):
    """
    Kamu İlan (SBB) ve diğer PDF ilanlarını resmi sunucunun Referer korumasını
    aşarak doğrudan tarayıcıda açmak üzere proxy/stream eder.
    """
    job = db.get_job_by_id(job_id)
    if not job:
        return "İlan bulunamadı", 404
        
    url = job.get("url", "")
    if not url:
        return "İlan bağlantısı mevcut değil", 404

    # Eğer kamuilan.sbb.gov.tr ilanı ise, SBB'nin talep ettiği Referer başlığıyla çek
    if "kamuilan.sbb.gov.tr" in url.lower():
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://kamuilan.sbb.gov.tr/",
                "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8"
            }
            resp = requests.get(url, headers=headers, verify=False, timeout=25, stream=True)
            
            # Content-Disposition başlığından orijinal dosya adını al
            content_disp = resp.headers.get("Content-Disposition", "")
            filename = "kamu_ilan_resmi.pdf"
            if "filename=" in content_disp:
                fname_part = content_disp.split("filename=")[-1].strip('"\'; ')
                if fname_part:
                    filename = fname_part
                    
            content_type = resp.headers.get("Content-Type", "application/pdf")
            
            def generate():
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk:
                        yield chunk
                        
            return Response(
                generate(),
                content_type=content_type,
                headers={
                    "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
                    "Cache-Control": "public, max-age=3600"
                }
            )
        except Exception as e:
            logger.error(f"SBB PDF proxy hatası (job_id={job_id}, url={url}): {e}")
            return redirect(url)
            
    elif "iskur.gov.tr" in url.lower() and ".pdf" in url.lower():
        try:
            import urllib.parse
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://www.iskur.gov.tr/",
                "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8"
            }
            parts = urllib.parse.urlsplit(url)
            safe_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, urllib.parse.quote(parts.path), parts.query, parts.fragment))
            resp = requests.get(safe_url, headers=headers, timeout=25, stream=True)
            return Response(
                resp.iter_content(chunk_size=16384),
                content_type="application/pdf",
                headers={
                    "Content-Disposition": "inline; filename=iskur_ilan.pdf",
                    "Cache-Control": "public, max-age=3600"
                }
            )
        except Exception as e:
            logger.error(f"İŞKUR PDF proxy hatası (job_id={job_id}, url={url}): {e}")
            return redirect(url)
            
    # Diğer ilanlar için orijinal linke yönlendir
    return redirect(url)

@app.route("/api/view-pdf")
def api_view_pdf_by_url():
    """URL parametresi ile verilen SBB PDF linkini stream eder."""
    url = request.args.get("url", "")
    if not url:
        return "URL belirtilmedi", 400
        
    if "kamuilan.sbb.gov.tr" in url.lower():
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://kamuilan.sbb.gov.tr/",
            }
            resp = requests.get(url, headers=headers, verify=False, timeout=25, stream=True)
            return Response(
                resp.iter_content(chunk_size=16384),
                content_type=resp.headers.get("Content-Type", "application/pdf"),
                headers={"Content-Disposition": "inline; filename=kamu_ilan.pdf"}
            )
        except Exception as e:
            logger.error(f"SBB PDF stream hatası ({url}): {e}")
            return redirect(url)
            
    return redirect(url)

@app.route("/api/export-csv")
def api_export_csv():
    """Tüm ilanları CSV dosyası olarak indirir."""
    csv_data = db.export_jobs_to_csv()
    filename = f"kpss_ilanlar_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route("/api/stats")
def api_get_stats():
    """Sistem ve ilan istatistikleri."""
    stats = db.get_stats()
    return jsonify({"status": "success", "stats": stats})

@app.route("/api/scan", methods=["POST"])
def api_manual_scan():
    """Kullanıcının web panelinden tetiklediği manuel tarama (Arka plan asenkron thread)."""
    global is_scanning
    if is_scanning:
        return jsonify({
            "status": "busy",
            "message": "Tarama işlemi şu anda devam ediyor. Lütfen tamamlanmasını bekleyin."
        })

    thread = threading.Thread(target=perform_scan_and_notify, kwargs={"is_manual": True}, daemon=True)
    thread.start()
    return jsonify({
        "status": "started",
        "message": "Tarama arka planda başlatıldı."
    })

@app.route("/api/scan/status")
def api_scan_status():
    """Taramanın güncel durumunu ve son tarama kaydını döner."""
    stats = db.get_stats()
    return jsonify({
        "status": "success",
        "is_scanning": is_scanning,
        "last_scan": stats.get("last_scan")
    })

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    """Telegram test bildirimi devre dışı bırakıldı."""
    return jsonify({
        "status": "info",
        "message": "Test bildirimleri devre dışı bırakılmıştır. İlanlar yalnızca günde 1 kez toplu bülten olarak iletilmektedir."
    })

@app.route("/api/send-digest", methods=["POST"])
def api_trigger_digest():
    """Bekleyen ilanlar için manuel günlük bülten tetikler."""
    unnotified_jobs = db.get_unnotified_jobs()
    if not unnotified_jobs:
        return jsonify({"status": "info", "message": "Bildirilecek bekleyen yeni ilan bulunmuyor."})

    success, count = telegram_notifier.send_daily_digest(unnotified_jobs)
    if success:
        db.mark_as_notified([j["id"] for j in unnotified_jobs])
        return jsonify({"status": "success", "message": f"{count} ilan Telegram'a iletildi."})
    else:
        return jsonify({"status": "error", "message": "Telegram mesajı gönderilemedi."})

# --- ZAMANLAYICI VE BAŞLANGIÇ ---

def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        perform_scan_and_notify,
        "interval",
        minutes=SCAN_INTERVAL_MINUTES,
        id="periodic_job_scan",
        replace_existing=True
    )
    scheduler.add_job(
        scheduled_daily_digest,
        "cron",
        hour=DAILY_NOTIFICATION_HOUR,
        minute=DAILY_NOTIFICATION_MINUTE,
        id="daily_job_digest",
        replace_existing=True
    )
    scheduler.start()
    logger.info(f"APScheduler başlatıldı. Tarama periyodu: {SCAN_INTERVAL_MINUTES} dk, Günlük bülten: {DAILY_NOTIFICATION_HOUR:02d}:{DAILY_NOTIFICATION_MINUTE:02d}")

def check_initial_scan():
    """Eğer sistem ilk kez ayağa kalktıysa ve veritabanında hiç ilan yoksa,
    kullanıcı müdahalesi beklemeden arka planda ilk taramayı otonom olarak başlatır."""
    def _initial_scan_worker():
        import time
        time.sleep(10)  # Sunucunun başlaması için 10 saniye bekle
        try:
            stats = db.get_stats()
            if stats.get("total_jobs", 0) == 0:
                logger.info("Veritabanı boş tespit edildi. İlk otonom tarama başlatılıyor...")
                perform_scan_and_notify()
        except Exception as e:
            logger.error(f"İlk tarama kontrolü hatası: {e}")

    threading.Thread(target=_initial_scan_worker, daemon=True, name="InitialScanThread").start()

# Veritabanını ilklendir
db.init_db()

if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or os.environ.get("RUN_SCHEDULER", "true").lower() == "true":
    start_scheduler()
    check_initial_scan()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"KPSS Radar sunucusu başlatılıyor: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
