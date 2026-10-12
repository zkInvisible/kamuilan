"""SQLite storage. Existing databases migrate without deleting user records."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from deadlines import parse_deadline_info
from eligibility import fold

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "kpss_jobs.db"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            institution TEXT DEFAULT '', url TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
            summary TEXT DEFAULT '', tags TEXT DEFAULT '[]', published_date TEXT DEFAULT '',
            created_at TEXT NOT NULL, detail_cache TEXT, signature TEXT DEFAULT ''
        )""")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        for name, definition in (
            ("detail_cache", "TEXT"), ("signature", "TEXT DEFAULT ''"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_signature ON jobs(signature)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)")
        connection.execute("""CREATE TABLE IF NOT EXISTS scan_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
            total_scraped INTEGER DEFAULT 0, new_inserted INTEGER DEFAULT 0,
            status TEXT, error_message TEXT DEFAULT '', sources_json TEXT DEFAULT '{}'
        )""")
        log_columns = {row["name"] for row in connection.execute("PRAGMA table_info(scan_logs)")}
        if "sources_json" not in log_columns:
            connection.execute("ALTER TABLE scan_logs ADD COLUMN sources_json TEXT DEFAULT '{}'")


def signature(job):
    text = re.sub(r"([a-zçğıöşü])([A-ZÇĞİÖŞÜ])", r"\1 \2", f"{job.get('institution', '')} {job.get('title', '')}")
    stop = {"alimi", "alacak", "ilani", "ilan", "baskanligi", "mudurlugu", "ve", "icin"}
    words = sorted(set(word for word in re.findall(r"[a-z0-9]+", fold(text)) if word not in stop))
    published = job.get("published_date", "")
    deadline = parse_deadline_info(published, summary=job.get("summary", ""))
    period = deadline.get("deadline_iso") or published
    # Undated notices in different years must not overwrite each other.
    period = period or str(datetime.now(timezone.utc).year)
    return hashlib.sha256((" ".join(words) + "|" + period).encode()).hexdigest()


def insert_job(job):
    fingerprint = signature(job)
    with get_connection() as connection:
        existing = connection.execute("SELECT id, source, url FROM jobs WHERE url = ? OR signature = ? ORDER BY url = ? DESC LIMIT 1",
                                      (job["url"], fingerprint, job["url"])).fetchone()
        if existing:
            # SBB links change their encrypted query strings. Keep the latest working URL.
            same_source = existing["source"] == job["source"]
            prefer_official = job["source"] == "Kamu İlan (SBB)"
            if same_source or prefer_official:
                connection.execute("UPDATE jobs SET title=?, institution=?, url=?, source=?, summary=?, published_date=?, signature=? WHERE id=?",
                    (job["title"], job.get("institution", ""), job["url"], job["source"], job.get("summary", ""),
                     job.get("published_date", ""), fingerprint, existing["id"]))
            return existing["id"], False
        cursor = connection.execute("INSERT INTO jobs (title,institution,url,source,summary,tags,published_date,created_at,signature) VALUES (?,?,?,?,?,?,?,?,?)",
            (job["title"], job.get("institution", ""), job["url"], job["source"], job.get("summary", ""),
             json.dumps(job.get("tags", []), ensure_ascii=False), job.get("published_date", ""), now_iso(), fingerprint))
        return cursor.lastrowid, True


def decode(row):
    if row is None:
        return None
    item = dict(row)
    for key, default in (("tags", []), ("detail_cache", None)):
        try:
            item[key] = json.loads(item.get(key) or "null") or default
        except (ValueError, TypeError):
            item[key] = default
    # Old notification/favorite columns remain on disk for safe migration, but are unused.
    for key in ("is_notified", "notified_at", "is_favorite", "requires_experience", "experience_note"):
        item.pop(key, None)
    return item


def get_jobs(limit=1000):
    with get_connection() as connection:
        return [decode(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


def get_job_by_id(job_id):
    with get_connection() as connection:
        return decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def save_detail(job_id, detail, tags):
    with get_connection() as connection:
        connection.execute("UPDATE jobs SET detail_cache=?, tags=? WHERE id=?",
                           (json.dumps(detail, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), job_id))


def log_scan(started_at, total_scraped, new_inserted, status, sources=None, error_message=""):
    with get_connection() as connection:
        connection.execute("INSERT INTO scan_logs (started_at,finished_at,total_scraped,new_inserted,status,error_message,sources_json) VALUES (?,?,?,?,?,?,?)",
                           (started_at, now_iso(), total_scraped, new_inserted, status, error_message, json.dumps(sources or {}, ensure_ascii=False)))
        # Keep diagnostic history small on a free instance.
        connection.execute("DELETE FROM scan_logs WHERE id NOT IN (SELECT id FROM scan_logs ORDER BY id DESC LIMIT 50)")


def last_scan():
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM scan_logs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["sources"] = json.loads(item.pop("sources_json", "") or "{}")
    except ValueError:
        item["sources"] = {}
    return item


def delete_expired_jobs():
    expired = []
    for job in get_jobs(limit=10000):
        cached = job.get("detail_cache") or {}
        # Recompute day counts from explicit evidence instead of trusting old badges.
        raw = cached.get("deadline_info") or {}
        info = parse_deadline_info("Son başvuru: " + raw["deadline_iso"], summary="") if raw.get("confidence") == "explicit" and raw.get("deadline_iso") else parse_deadline_info(
            job.get("published_date", ""), summary=job.get("summary", ""))
        if info.get("confidence") == "explicit" and info.get("badge_type") == "expired":
            expired.append(job["id"])
    if expired:
        with get_connection() as connection:
            connection.executemany("DELETE FROM jobs WHERE id=?", [(job_id,) for job_id in expired])
    return len(expired)
