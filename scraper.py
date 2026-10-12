"""Public job sources and bounded document extraction for Render Free."""
import hashlib
import io
import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

import pypdf
import requests
from bs4 import BeautifulSoup

import eligibility
from sbb_client import fetch_sbb_pdf, is_sbb_url, MAX_PDF_BYTES

logger = logging.getLogger(__name__)
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "tr-TR,tr;q=0.9"}
MAX_HTML_BYTES = 4 * 1024 * 1024


def html_soup(content):
    # Some legacy Turkish pages omit their charset; Latin-1 guesses corrupt I/ş/ğ.
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1254", errors="replace")
    return BeautifulSoup(text, "html.parser")


def fetch(url, *, method="GET", payload=None, limit=MAX_HTML_BYTES, referer=None):
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    with requests.request(method, url, json=payload, headers=headers, timeout=(5, 15), stream=True) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(16384):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Kaynak belge boyut sınırını aşıyor")
        return bytes(content), response.url, dict(response.headers)


def classify_job(title, institution="", summary="", full_text="", **_):
    text = eligibility.fold(f"{title} {institution} {summary} {full_text}")
    tags = []
    if "makine muhendis" in text or "makina muhendis" in text:
        tags.append("MAKINE_MUHENDIS")
    elif re.search(r"memur|buro personel|vhki|bilgisayar isletmen", text):
        tags.append("BANA_UYGUN_DIGER")
    for pattern, tag in (
        (r"belediye|mahalli idare", "BELEDIYE_MAHALLI"),
        (r"bakanli|genel mudur|kamu iktisadi|399|\bkit\b", "KIT_KURUMLARI"),
        (r"sozlesmeli|4[/\s-]?b", "SOZLESMELI_4B"),
        (r"uzman yardim|mufettis|denetci|kariyer", "KARIYER_MESLEK"),
        (r"universite", "UNIVERSITE_KADRO"),
    ):
        if re.search(pattern, text):
            tags.append(tag)
    return tags


def build_job_item(title, institution, url, source, summary="", published_date=""):
    title = re.sub(r"\s+", " ", title).strip()
    repeated = re.match(r"^(.{12,}?)\s*\1[.\s]*$", title)
    if repeated:
        title = repeated[1].strip()
    return {"title": title[:350], "institution": institution[:180], "url": url,
            "source": source, "summary": summary[:1500] or title,
            "published_date": published_date, "tags": classify_job(title, institution, summary)}


def guess_institution(title):
    match = re.search(r"^(.+?(?:Belediyesi|Belediye Başkanlığı|Üniversitesi|Bakanlığı|Başkanlığı|Genel Müdürlüğü))", title, re.I)
    return match[1] if match else title[:100]


def scrape_memurlar_net():
    base = "https://ilan.memurlar.net/"
    try:
        content, _, _ = fetch(base)
    except requests.HTTPError as error:
        # Some network edges block the short homepage route while serving the
        # site's regular public listings page. Retry that page once on 403.
        if error.response is None or error.response.status_code != 403:
            raise
        base = urljoin(base, "default.aspx?page=1")
        content, _, _ = fetch(base)
    soup = html_soup(content)
    jobs, seen = [], set()
    for anchor in soup.select("a[href*='/ilan/']"):
        url = urljoin(base, anchor["href"])
        title = anchor.get_text(" ", strip=True)
        if len(title) < 15 or url in seen:
            continue
        seen.add(url)
        jobs.append(build_job_item(title, guess_institution(title), url, "Memurlar.Net", title))
    return jobs


def scrape_kamuilan_sbb():
    base = "https://kamuilan.sbb.gov.tr/"
    content, _, _ = fetch(base)
    soup = html_soup(content)
    jobs, seen = [], set()
    for anchor in soup.find_all("a", href=lambda value: value and "ilandetay.aspx" in value.lower()):
        url = urljoin(base, anchor["href"])
        raw = anchor.get_text(" ", strip=True)
        if len(raw) < 15 or url in seen:
            continue
        seen.add(url)
        period = re.search(r"\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s*[-–]\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+(?:\s+20\d{2})?", raw)
        date_field = "Başvuru: " + period[0] if period else ""
        title = raw.replace(period[0], "").strip(" ()") if period else raw
        jobs.append(build_job_item(title, guess_institution(title), url, "Kamu İlan (SBB)", f"{title} | {date_field}", date_field))
    return jobs


def scrape_resmi_gazete():
    base = "https://www.resmigazete.gov.tr/"
    today = datetime.now(ZoneInfo("Europe/Istanbul"))
    jobs = []
    for offset in range(3 if today.weekday() == 0 else 2):
        day = today - timedelta(days=offset)
        url = f"{base}ilanlar/eskiilanlar/{day:%Y}/{day:%m}/{day:%Y%m%d}-4.htm"
        try:
            content, _, _ = fetch(url)
        except requests.HTTPError as error:
            if error.response.status_code == 404:
                continue
            raise
        soup = html_soup(content)
        for paragraph in soup.find_all("p"):
            anchor = paragraph.find("a", href=True)
            title = paragraph.get_text(" ", strip=True)
            if not anchor or len(title) < 15 or not urlsplit(anchor["href"]).path.lower().endswith(".pdf"):
                continue
            if eligibility.prefilter(title):
                continue
            jobs.append(build_job_item(title, title.split(":")[0], urljoin(url, anchor["href"]), "Resmi Gazete", title, day.strftime("%d.%m.%Y")))
    return jobs


def scrape_basin_ilan_kurumu():
    base = "https://www.ilan.gov.tr"
    jobs = []
    for skip in (0, 20):
        content, _, _ = fetch(base + "/api/api/services/app/Ad/AdsByFilter", method="POST",
                              payload={"skipCount": skip, "maxResultCount": 20, "keys": {"ats": ["5"]}},
                              referer=base + "/personel-alim-ilanlari")
        data = json.loads(content).get("result") or {}
        for advert in data.get("ads", []):
            title, path = advert.get("title", ""), advert.get("urlStr", "")
            if not title or not path:
                continue
            jobs.append(build_job_item(title, advert.get("advertiserName", ""), urljoin(base, path), "Basın İlan Kurumu",
                                       title, (advert.get("publishStartDate") or "")[:10]))
    return jobs


def scrape_savunma_msb():
    jobs = []
    sources = [
        ("https://www.ssb.gov.tr/duyurular", "Savunma Sanayii Başkanlığı"),
        ("https://personeltemin.msb.gov.tr/", "Millî Savunma Bakanlığı"),
    ]
    for base, institution in sources:
        try:
            content, _, _ = fetch(base)
        except Exception as error:
            logger.warning("%s kaynağı okunamadı: %s", institution, error)
            continue
        soup = html_soup(content)
        links = [(anchor.get_text(" ", strip=True), anchor["href"]) for anchor in soup.find_all("a", href=True)]
        # MSB renders public announcement cards as onclick links, not anchors.
        for card in soup.select("[onclick]"):
            link = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", card["onclick"])
            heading = card.find(["h2", "h3", "h4"])
            if link and heading:
                links.append((heading.get_text(" ", strip=True), link[1]))
        seen = set()
        announcement_count = 0
        for title, path in links:
            url = urljoin(base, path)
            if urlsplit(url).scheme not in {"https", "http"} or url in seen:
                continue
            seen.add(url)
            if institution == "Millî Savunma Bakanlığı" and "/duyurudetay/" not in url.lower():
                continue
            if institution == "Savunma Sanayii Başkanlığı":
                if "/duyuru/" not in url.lower():
                    continue
                announcement_count += 1
                if announcement_count > 24:
                    break
            if len(title) < 15 or not re.search(r"personel|memur|muhendis|uzman|kadro|sinav", eligibility.fold(title)):
                continue
            if eligibility.prefilter(title):
                continue
            jobs.append(build_job_item(title, institution, url, "Savunma Sanayii / MSB", title))
    return jobs


def scrape_iskur_kamu():
    # Follow only provinces with active civil-servant notices; no geographic priority.
    base = "https://www.iskur.gov.tr/ilanlar/kamu-memur-alim-ilanlari/"
    content, _, _ = fetch(base)
    soup = html_soup(content)
    pages = [soup]
    active = [(node.get("id"), node.get("data-iladi")) for node in soup.select("g[data-count]")
              if str(node.get("data-count", "")).isdigit() and int(node["data-count"]) > 0
              and node.get("id") and node.get("data-iladi")]
    # Bound requests even if all 81 provinces have notices; rotate remaining provinces.
    if len(active) > 24:
        offset = (int(datetime.now().timestamp()) // (8 * 3600) * 24) % len(active)
        active = (active + active)[offset:offset + 24]
        logger.warning("İŞKUR: bu turda en fazla 24 aktif il sayfası okunur; kalanlar sonraki turlara döner")

    def province_page(province):
        url = base + "?" + urlencode({"idId": province[0], "il": province[1]})
        try:
            body, _, _ = fetch(url)
            return html_soup(body), None
        except Exception as error:
            return None, f"İŞKUR {province[1]}: {error}"

    for province in active:
        page, error = province_page(province)
        if error:
            logger.warning(error)
        else:
            pages.append(page)
    jobs, seen = [], set()
    for page in pages:
        for anchor in page.find_all("a", href=True):
            url = urljoin(base, anchor["href"])
            if not urlsplit(url).path.lower().endswith(".pdf") or url in seen:
                continue
            row = anchor.find_parent("tr") or anchor.parent
            cells = row.find_all("td") if row else []
            title = cells[1].get_text(" ", strip=True) if len(cells) >= 2 else anchor.get("aria-label") or anchor.get_text(" ", strip=True)
            if len(title) < 10:
                continue
            seen.add(url)
            jobs.append(build_job_item(title, guess_institution(title), url, "İŞKUR Kamu İlanları", title))
    return jobs


class CaptureErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        import threading
        self.thread_id = threading.get_ident()
        self.messages = []

    def emit(self, record):
        if record.thread == self.thread_id:
            self.messages.append(record.getMessage()[:300])


def run_all_scrapers():
    sources = [
        ("Memurlar.Net", scrape_memurlar_net), ("Kamu İlan (SBB)", scrape_kamuilan_sbb),
        ("Resmi Gazete", scrape_resmi_gazete), ("Basın İlan Kurumu", scrape_basin_ilan_kurumu),
        ("Savunma Sanayii / MSB", scrape_savunma_msb), ("İŞKUR Kamu İlanları", scrape_iskur_kamu),
    ]

    def collect(item):
        name, scraper = item
        capture = CaptureErrors()
        logger.addHandler(capture)
        try:
            jobs = scraper()
            status = "partial" if capture.messages else "ok"
        except Exception as error:
            logger.warning("%s: %s", name, error)
            jobs, status = [], "error"
        finally:
            logger.removeHandler(capture)
        report = {"status": status, "collected": len(jobs), "error": "; ".join(capture.messages)[:500]}
        return name, jobs, report

    combined, reports = [], {}
    for source in sources:
        name, jobs, report = collect(source)
        combined.extend(jobs)
        reports[name] = report
    unique = {job["url"]: job for job in combined}
    return list(unique.values()), reports


def extract_pdf_document(content, max_pages=8):
    if not content.startswith(b"%PDF-"):
        raise ValueError("Kaynak gerçek bir PDF sunmadı")
    reader = pypdf.PdfReader(io.BytesIO(content))
    pages = reader.pages[:max_pages]
    text = "\n".join(page.extract_text() or "" for page in pages)[:120000]
    layout_pages = []
    for page in pages:
        try:
            layout_pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            layout_pages.append("")
    rows, general = eligibility.parse_layout_rows("\f".join(layout_pages)[:120000])
    return {"full_text": text, "structured_rows": rows, "general_text": general if rows else text,
            "document_verified": len(text.strip()) >= 80, "document_hash": hashlib.sha256(content).hexdigest()}


def pdf_bytes(url):
    if is_sbb_url(url):
        return fetch_sbb_pdf(url)
    content, _, _ = fetch(url, limit=MAX_PDF_BYTES)
    if not content.startswith(b"%PDF-"):
        raise ValueError("İlan kaynağı PDF yerine başka bir içerik döndürdü")
    return content


def fetch_job_detail(job):
    url = job["url"]
    if is_sbb_url(url) or urlsplit(url).path.lower().endswith(".pdf"):
        result = extract_pdf_document(pdf_bytes(url))
        result["document_url"] = url
    else:
        if "ilan.gov.tr" in urlsplit(url).netloc:
            match = re.search(r"/ilan/(\d+)", url)
            if not match:
                raise ValueError("İlan kimliği okunamadı")
            content, _, _ = fetch("https://www.ilan.gov.tr/api/api/services/app/AdDetail/GetAdDetail?id=" + match[1])
            advert = json.loads(content).get("result") or {}
            soup = BeautifulSoup(advert.get("content") or advert.get("legalBasisDetail") or "", "html.parser")
        else:
            content, _, _ = fetch(url)
            soup = html_soup(content)
        for node in soup(["script", "style", "nav", "header", "footer"]):
            node.decompose()
        article = soup.select_one("div.detail, article, .content, #content") or soup
        tables = [[[cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
                   for row in table.find_all("tr")] for table in article.find_all("table")]
        rows = eligibility.parse_html_rows(tables)
        full_text = article.get_text("\n", strip=True)[:120000]
        general_soup = BeautifulSoup(str(article), "html.parser")
        for table in general_soup.find_all("table"):
            table.decompose()
        general = general_soup.get_text("\n", strip=True) if rows else full_text
        result = {"full_text": full_text, "structured_rows": rows, "general_text": general,
                  "document_verified": len(full_text) >= 80, "document_hash": hashlib.sha256(full_text.encode()).hexdigest()}
        # Read one official linked PDF instead of using memory-intensive OCR.
        for anchor in article.find_all("a", href=True):
            document_url = urljoin(url, anchor["href"])
            host = urlsplit(document_url).hostname or ""
            if (is_sbb_url(document_url) or urlsplit(document_url).path.lower().endswith(".pdf")) and (
                host.endswith((".gov.tr", ".bel.tr", ".edu.tr", ".memurlar.net")) or host == "ilan.memurlar.net"
            ):
                try:
                    result = extract_pdf_document(pdf_bytes(document_url))
                    result["document_url"] = document_url
                    break
                except Exception:
                    logger.debug("Bağlı PDF okunamadı", exc_info=True)
    result["paragraphs"] = [line.strip() for line in result["full_text"].splitlines() if line.strip()][:80]
    return result
