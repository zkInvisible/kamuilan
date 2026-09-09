import sqlite3
import json
import os
import csv
import io
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kpss_jobs.db"))

def turkish_lower(text: str) -> str:
    """Türkçe karakterleri güvenli bir şekilde küçük harfe çevirir."""
    if not text:
        return ""
    char_map = {"İ": "i", "I": "ı", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"}
    for u, l in char_map.items():
        text = text.replace(u, l)
    return text.lower()

def generate_job_signature(title: str, institution: str = "") -> str:
    """İlanın kurum, kadro sayısı ve temel unvanlarından benzersiz çapraz kaynak parmak izi oluşturur."""
    raw_text = f"{institution} {title}"
    # Kelime yapışıklıklarını (camelCase, sayı-harf bitişiklikleri vb.) ayır:
    raw_text = re.sub(r'([a-zçğıöşü0-9])([A-ZÇĞİÖŞÜ])', r'\1 \2', raw_text)
    raw_text = re.sub(r'([a-zA-ZçğıöşüÇĞİÖŞÜ])([0-9])', r'\1 \2', raw_text)
    raw_text = re.sub(r'([0-9])([a-zA-ZçğıöşüÇĞİÖŞÜ])', r'\1 \2', raw_text)

    combined = turkish_lower(raw_text)
    numbers = re.findall(r'\b\d+\b', combined)
    stop_words = {
        "alimi", "alımı", "alacak", "ilani", "ilanı", "personel", "sozlesmeli", 
        "sözleşmeli", "rektörlüğünden", "rektorlugunden", "duyurusu", "baskanligi", 
        "başkanlığı", "mudurlugu", "müdürlüğü", "bakanligi", "bakanlığı", 
        "universitesi", "üniversitesi", "genel", "ilan", "ve", "ile", "icin", "için",
        "memur", "kadro", "birlik", "belediye", "belediyesi"
    }
    cleaned = re.sub(r'[^a-z0-9çğıöşü\s]', ' ', combined)
    words = [w for w in cleaned.split() if len(w) > 2 and w not in stop_words and not w.isdigit()]
    distinct_words = sorted(list(set(words)))
    sig_words = " ".join(distinct_words[:6])
    sig_numbers = "-".join(sorted(list(set(numbers))))
    return f"{sig_words}|{sig_numbers}" if sig_words else ""

def merge_job_cluster(jobs_in_cluster: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Farklı kaynaklardaki aynı ilanın verilerini tek bir zengin ilanda birleştirir."""
    if not jobs_in_cluster:
        return {}
    if len(jobs_in_cluster) == 1:
        item = dict(jobs_in_cluster[0])
        if isinstance(item.get("tags"), str):
            try:
                item["tags"] = json.loads(item["tags"])
            except Exception:
                item["tags"] = []
        item["all_sources"] = [item.get("source", "")]
        is_sbb_pdf = "kamuilan.sbb" in item.get("url", "").lower()
        item["has_pdf"] = is_sbb_pdf
        item["pdf_url"] = f"/api/jobs/{item['id']}/pdf" if is_sbb_pdf else None
        item["web_url"] = item.get("url")
        item["source_links"] = [{
            "id": item.get("id"),
            "source": item.get("source"),
            "url": item.get("url"),
            "is_pdf": is_sbb_pdf
        }]
        return item

    # SBB ilanı var mı? (PDF doğrudan açılış için)
    sbb_job = next((j for j in jobs_in_cluster if "kamuilan.sbb" in j.get("url", "").lower() or "sbb" in j.get("source", "").lower()), None)
    # Memurlar.Net, BİK veya Resmi Gazete var mı?
    web_job = next((j for j in jobs_in_cluster if "kamuilan.sbb" not in j.get("url", "").lower()), None)

    # Birincil ilan: En temiz, tekrarsız başlıklı olanı seç
    cleanest_job = min(jobs_in_cluster, key=lambda j: len(j.get("title", "")))
    primary = dict(cleanest_job)

    # Tüm kaynakları ve bağlantıları topla
    all_sources = []
    source_links = []
    for j in jobs_in_cluster:
        s = j.get("source", "")
        if s and s not in all_sources:
            all_sources.append(s)
        is_pdf = "kamuilan.sbb" in j.get("url", "").lower()
        source_links.append({
            "id": j.get("id"),
            "source": s,
            "url": j.get("url"),
            "is_pdf": is_pdf
        })

    primary["all_sources"] = all_sources
    primary["source_links"] = source_links
    primary["source"] = " • ".join(all_sources)

    # Doğrudan PDF linki (SBB varsa SBB'nin proxy linki atanır)
    if sbb_job:
        primary["pdf_url"] = f"/api/jobs/{sbb_job['id']}/pdf"
        primary["has_pdf"] = True
    else:
        primary["has_pdf"] = False

    # Web URL (Memurlar.Net veya Resmi Gazete linki)
    if web_job:
        primary["web_url"] = web_job.get("url")
    else:
        primary["web_url"] = primary.get("url")

    # Tüm etiketlerin birleşimi
    all_tags = set()
    for j in jobs_in_cluster:
        t_raw = j.get("tags", [])
        if isinstance(t_raw, str):
            try:
                t_raw = json.loads(t_raw)
            except Exception:
                t_raw = []
        all_tags.update(t_raw)
    primary["tags"] = list(all_tags)

    # Favori durumu: Herhangi birinde favorilenmişse favori kalır
    primary["is_favorite"] = 1 if any(j.get("is_favorite") == 1 for j in jobs_in_cluster) else 0

    # Tecrübe şartı: Herhangi birinde tecrübe şartı varsa 1
    primary["requires_experience"] = 1 if any(j.get("requires_experience") == 1 for j in jobs_in_cluster) else 0

    # Önbellek: detail_cache dolu olanı al
    for j in jobs_in_cluster:
        if j.get("detail_cache"):
            primary["detail_cache"] = j["detail_cache"]
            break

    return primary

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Veritabanı tablolarını ve indekslerini ilklendirir, migration'ları uygular."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        institution TEXT DEFAULT '',
        url TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL,
        summary TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        published_date TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        is_notified INTEGER DEFAULT 0,
        notified_at TEXT DEFAULT NULL,
        is_favorite INTEGER DEFAULT 0
    );
    """)

    # Var olan veritabanlarında is_favorite, detail_cache, requires_experience kolonları yoksa ekle (Migration)
    cursor.execute("PRAGMA table_info(jobs);")
    columns = [col["name"] for col in cursor.fetchall()]
    if "is_favorite" not in columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN is_favorite INTEGER DEFAULT 0;")
    if "detail_cache" not in columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN detail_cache TEXT DEFAULT NULL;")
    if "requires_experience" not in columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN requires_experience INTEGER DEFAULT 0;")
    if "experience_note" not in columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN experience_note TEXT DEFAULT '';")
    if "signature" not in columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN signature TEXT DEFAULT '';")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_notified ON jobs(is_notified);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_favorite ON jobs(is_favorite);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_experience ON jobs(requires_experience);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_signature ON jobs(signature);")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scan_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        total_scraped INTEGER DEFAULT 0,
        new_inserted INTEGER DEFAULT 0,
        status TEXT DEFAULT 'SUCCESS',
        error_message TEXT DEFAULT ''
    );
    """)

    conn.commit()
    conn.close()

def insert_job(job_data: Dict[str, Any]) -> Tuple[Optional[int], bool]:
    """
    Yeni bir ilanı veritabanına ekler.
    Eğer URL veya aynı ilanın çapraz kaynak imzası (signature) zaten kayıtlıysa mükerrer eklemez.
    Dönüş: (id, is_new)
    """
    conn = get_connection()
    cursor = conn.cursor()

    clean_url = job_data.get("url", "").strip()
    # 1. URL kontrolü
    cursor.execute("SELECT id FROM jobs WHERE url = ?", (clean_url,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row["id"], False

    tags_list = job_data.get("tags") or []
    is_uygun_degil = "UYGUN_DEGIL" in tags_list

    # 2. Çapraz kaynak tekilleştirme kontrolü (Yalnızca geçerli ilanlar için yapılır, elenen ilanların URL'leri kaydedilmelidir)
    sig = ""
    if not is_uygun_degil:
        sig = generate_job_signature(job_data.get("title", ""), job_data.get("institution", ""))
        if sig:
            cursor.execute("SELECT id, source, url FROM jobs WHERE signature = ? AND signature != ''", (sig,))
            existing_sig = cursor.fetchone()
            if existing_sig:
                # Aynı ilan farklı bir kaynaktan zaten mevcut! Mükerrer ekleme
                conn.close()
                return existing_sig["id"], False

    tags_json = json.dumps(job_data.get("tags", []), ensure_ascii=False)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    req_exp = 1 if job_data.get("requires_experience") else 0
    exp_note = job_data.get("experience_note", "").strip()

    try:
        cursor.execute("""
            INSERT INTO jobs (title, institution, url, source, summary, tags, published_date, created_at, is_notified, is_favorite, requires_experience, experience_note, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
        """, (
            job_data.get("title", "").strip(),
            job_data.get("institution", "").strip(),
            clean_url,
            job_data.get("source", "Bilinmeyen"),
            job_data.get("summary", "").strip(),
            tags_json,
            job_data.get("published_date", "").strip(),
            now_str,
            req_exp,
            exp_note,
            sig
        ))
        conn.commit()
        job_id = cursor.lastrowid
        conn.close()
        return job_id, True
    except sqlite3.IntegrityError:
        # URL zaten mevcut
        cursor.execute("SELECT id FROM jobs WHERE url = ?", (clean_url,))
        row = cursor.fetchone()
        existing_id = row["id"] if row else None
        conn.close()
        return existing_id, False
    except Exception as e:
        conn.close()
        raise e

def toggle_favorite(job_id: int) -> bool:
    """İlanın favori durumunu tersine çevirir (0 -> 1 veya 1 -> 0)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_favorite FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    new_state = 0 if row["is_favorite"] == 1 else 1
    cursor.execute("UPDATE jobs SET is_favorite = ? WHERE id = ?", (new_state, job_id))
    conn.commit()
    conn.close()
    return bool(new_state)

def get_jobs(
    tag: Optional[str] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    only_favorites: bool = False,
    sort_by: str = "newest",
    limit: int = 250,
    offset: int = 0,
    exclude_experience: bool = True
) -> List[Dict[str, Any]]:
    """İlanları etiket, arama, kaynak ve sıralama kriterlerine göre filtreleyip getirir."""
    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM jobs WHERE 1=1"
    params = []

    # Tecrübe Filtresi: Yeni mezun için tecrübe şartı olanlar kesinlikle elenir
    if exclude_experience:
        query += " AND (requires_experience = 0 OR requires_experience IS NULL) AND (tags NOT LIKE '%TECRUBE_SARTI%' OR tags IS NULL)"

    # Aday Uygunluk Filtresi: Adayın diplomasına/profiline uymayan ilanlar varsayılan olarak elenir
    if not tag or tag.upper() != "UYGUN_DEGIL":
        query += " AND (tags NOT LIKE '%UYGUN_DEGIL%' OR tags IS NULL)"

    if only_favorites:
        query += " AND is_favorite = 1"

    if tag and tag.upper() not in ["ALL", "FAVORITES"]:
        query += " AND tags LIKE ?"
        params.append(f"%{tag}%")

    if source and source.upper() != "ALL":
        query += " AND source = ?"
        params.append(source)

    if search and search.strip():
        term = f"%{search.strip()}%"
        query += " AND (title LIKE ? OR institution LIKE ? OR summary LIKE ?)"
        params.extend([term, term, term])

    # Sıralama mantığı: Her zaman en yeni eklenen ilanlar en üstte (ID azalan)
    query += " ORDER BY id DESC"

    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    clusters = {}
    for r in rows:
        item = dict(r)
        sig = item.get("signature") or generate_job_signature(item.get("title", ""), item.get("institution", ""))
        if not sig:
            sig = f"job_{item['id']}"
        clusters.setdefault(sig, []).append(item)

    merged_jobs = []
    for sig, cluster in clusters.items():
        merged_jobs.append(merge_job_cluster(cluster))

    conn.close()
    return merged_jobs

def get_job_by_id(job_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    try:
        item["tags"] = json.loads(item["tags"])
    except Exception:
        item["tags"] = []
    return item

def update_job_detail_cache(job_id: int, detail_data: Dict[str, Any]) -> None:
    """İlanın detaylı analiz ve içerik verisini önbelleğe kaydeder."""
    conn = get_connection()
    cursor = conn.cursor()
    detail_json = json.dumps(detail_data, ensure_ascii=False)
    cursor.execute("UPDATE jobs SET detail_cache = ? WHERE id = ?", (detail_json, job_id))
    conn.commit()
    conn.close()

def get_job_detail_cache(job_id: int) -> Optional[Dict[str, Any]]:
    """İlanın önbellekteki detay verisini döner. Kümedeki diğer eşleşen kaynakların önbelleğini de değerlendirir."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT signature, detail_cache FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if row and row["detail_cache"]:
        conn.close()
        try:
            return json.loads(row["detail_cache"])
        except Exception:
            return None

    if row and row["signature"]:
        cursor.execute("SELECT detail_cache FROM jobs WHERE signature = ? AND detail_cache IS NOT NULL AND id != ?", (row["signature"], job_id))
        cluster_rows = cursor.fetchall()
        for cr in cluster_rows:
            if cr["detail_cache"]:
                conn.close()
                try:
                    return json.loads(cr["detail_cache"])
                except Exception:
                    pass

    conn.close()
    return None

def update_job_experience_status(job_id: int, requires_exp: bool, note: str = "") -> None:
    """İlanın tecrübe şartı durumunu ve etiketlerini günceller."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT tags FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return
    try:
        tags = json.loads(row["tags"])
    except Exception:
        tags = []
    
    if requires_exp:
        if "TECRUBE_SARTI" not in tags:
            tags.append("TECRUBE_SARTI")
    else:
        tags = [t for t in tags if t != "TECRUBE_SARTI"]
        
    tags_json = json.dumps(tags, ensure_ascii=False)
    cursor.execute(
        "UPDATE jobs SET requires_experience = ?, experience_note = ?, tags = ? WHERE id = ?",
        (1 if requires_exp else 0, note, tags_json, job_id)
    )
    conn.commit()
    conn.close()

def update_job_tags(job_id: int, tags: List[str]) -> None:
    """İlanın etiketlerini günceller."""
    conn = get_connection()
    cursor = conn.cursor()
    tags_json = json.dumps(tags, ensure_ascii=False)
    cursor.execute("UPDATE jobs SET tags = ? WHERE id = ?", (tags_json, job_id))
    conn.commit()
    conn.close()

def get_unnotified_jobs() -> List[Dict[str, Any]]:
    """Henüz Telegram ile bildirilmemiş, tecrübe şartı aranmayan ve adaya uygun ilanları getirir."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM jobs 
        WHERE is_notified = 0 
          AND (requires_experience = 0 OR requires_experience IS NULL) 
          AND (tags NOT LIKE '%TECRUBE_SARTI%' OR tags IS NULL)
          AND (tags NOT LIKE '%UYGUN_DEGIL%' OR tags IS NULL)
        ORDER BY id ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    jobs = []
    for r in rows:
        item = dict(r)
        try:
            item["tags"] = json.loads(item["tags"])
        except Exception:
            item["tags"] = []
        jobs.append(item)
    return jobs

def mark_as_notified(job_ids: List[int]) -> None:
    """Belirtilen ilanları bildirildi olarak işaretler."""
    if not job_ids:
        return
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    placeholders = ",".join("?" for _ in job_ids)
    cursor.execute(
        f"UPDATE jobs SET is_notified = 1, notified_at = ? WHERE id IN ({placeholders})",
        [now_str] + job_ids
    )
    conn.commit()
    conn.close()

def log_scan(
    started_at: str,
    finished_at: str,
    total_scraped: int,
    new_inserted: int,
    status: str = "SUCCESS",
    error_message: str = ""
) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scan_logs (started_at, finished_at, total_scraped, new_inserted, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (started_at, finished_at, total_scraped, new_inserted, status, error_message))
    conn.commit()
    conn.close()

def get_stats() -> Dict[str, Any]:
    """İlan istatistiklerini ve etiket sayılarını döner (Tecrübe şartı olanlar filtrelenir)."""
    conn = get_connection()
    cursor = conn.cursor()

    exp_filter = " AND (requires_experience = 0 OR requires_experience IS NULL) AND (tags NOT LIKE '%TECRUBE_SARTI%' OR tags IS NULL) AND (tags NOT LIKE '%UYGUN_DEGIL%' OR tags IS NULL)"

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE 1=1 {exp_filter}")
    total_jobs = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) AS total FROM jobs WHERE requires_experience = 1 OR tags LIKE '%TECRUBE_SARTI%'")
    tecrube_sarti_sayisi = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%MAKINE_MUHENDIS%' {exp_filter}")
    makine_muhendis = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%SAMSUN_HEPSI%' {exp_filter}")
    samsun_hepsi = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%CEVRE_ILLER%' {exp_filter}")
    cevre_iller = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%KIT_KURUMLARI%' {exp_filter}")
    kit_kurumlari = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%BELEDIYE_MAHALLI%' {exp_filter}")
    belediye_mahalli = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%UNIVERSITE_KADRO%' {exp_filter}")
    universite_kadro = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%KARIYER_MESLEK%' {exp_filter}")
    kariyer_meslek = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%SOZLESMELI_4B%' {exp_filter}")
    sozlesmeli_4b = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE tags LIKE '%BANA_UYGUN_DIGER%' {exp_filter}")
    bana_uygun = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE is_favorite = 1 {exp_filter}")
    favorites_count = cursor.fetchone()["total"]

    cursor.execute(f"SELECT COUNT(*) AS total FROM jobs WHERE is_notified = 0 {exp_filter}")
    pending_notifications = cursor.fetchone()["total"]

    cursor.execute("SELECT * FROM scan_logs ORDER BY id DESC LIMIT 1")
    last_log = cursor.fetchone()

    conn.close()

    return {
        "total_jobs": total_jobs,
        "tecrube_sarti_elenen": tecrube_sarti_sayisi,
        "makine_muhendis": makine_muhendis,
        "samsun_hepsi": samsun_hepsi,
        "cevre_iller": cevre_iller,
        "kit_kurumlari": kit_kurumlari,
        "belediye_mahalli": belediye_mahalli,
        "universite_kadro": universite_kadro,
        "kariyer_meslek": kariyer_meslek,
        "sozlesmeli_4b": sozlesmeli_4b,
        "bana_uygun_diger": bana_uygun,
        "favorites_count": favorites_count,
        "pending_notifications": pending_notifications,
        "last_scan": dict(last_log) if last_log else None
    }

def reclassify_all_jobs(classify_fn) -> int:
    """Mevcut tüm kayıtları güncel etiketleme kurallarıyla tekrar etiketler."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, institution, summary FROM jobs")
    rows = cursor.fetchall()
    count = 0
    for r in rows:
        new_tags = classify_fn(title=r["title"], institution=r["institution"], summary=r["summary"])
        tags_json = json.dumps(new_tags, ensure_ascii=False)
        cursor.execute("UPDATE jobs SET tags = ? WHERE id = ?", (tags_json, r["id"]))
        count += 1
    conn.commit()
    conn.close()
    return count

def export_jobs_to_csv() -> str:
    """Tüm ilanları UTF-8 Excel uyumlu CSV formatında metin olarak döner."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, institution, source, tags, published_date, created_at, url FROM jobs ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    # Excel Türkçe karakterleri doğru açabilsin diye UTF-8 BOM ekliyoruz
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["ID", "Kurum", "İlan Başlığı", "Kaynak", "Etiketler", "Tarih/Son Başvuru", "Kayıt Tarihi", "İlan Linki"])

    for r in rows:
        tags_str = ", ".join(json.loads(r["tags"])) if r["tags"] else ""
        writer.writerow([
            r["id"],
            r["institution"],
            r["title"],
            r["source"],
            tags_str,
            r["published_date"],
            r["created_at"],
            r["url"]
        ])

    return output.getvalue()

def get_existing_urls() -> set:
    """Veritabanında kayıtlı olan tüm ilan URL'lerini hızlı sorgu için küme (set) olarak döner."""
    import urllib.parse
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM jobs WHERE url IS NOT NULL AND url != ''")
    rows = cursor.fetchall()
    conn.close()
    urls = set()
    for r in rows:
        u = r["url"].strip()
        urls.add(u)
        urls.add(urllib.parse.unquote(u))
        urls.add(urllib.parse.quote(u, safe=":/"))
    return urls

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
