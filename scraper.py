import requests
from bs4 import BeautifulSoup
import urllib3
import re
import os
import io
import subprocess
import urllib.parse
import pypdf
from datetime import datetime, timedelta, date
from typing import List, Dict, Any, Tuple
import logging

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine = RapidOCR()
except Exception as e:
    _ocr_engine = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

# --- TÜRKÇE NORMALİZASYON VE ANAHTAR KELİME MOTORU ---

def turkish_lower(text: str) -> str:
    """Türkçe karakterleri güvenli bir şekilde küçük harfe çevirir ve Unicode birleştirmelerini temizler."""
    if not text:
        return ""
    # Unicode gizli karakterler ve birleştirme işaretleri (örn: i + \u0307 birleşim noktası)
    text = text.replace("\u0307", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").replace("\xa0", " ")
    char_map = {
        "İ": "i", "I": "ı", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"
    }
    for upper_c, lower_c in char_map.items():
        text = text.replace(upper_c, lower_c)
    return text.lower()

def normalize_for_search(text: str) -> str:
    """Arama ve kural eşleme için metni normalize eder (ı/i birleştirme, ASCII/Türkçe karmaşasını önleme)."""
    if not text:
        return ""
    t = turkish_lower(text)
    # i ve ı harflerini birleştirerek hem ASCII hem Türkçe büyük harf karmaşasını (JEOLOJI vs JEOLOJİ) çözer
    return t.replace("ı", "i")

TR_MONTHS = {
    "ocak": 1, "subat": 2, "şubat": 2, "mart": 3, "nisan": 4, "mayis": 5, "mayıs": 5,
    "haziran": 6, "temmuz": 7, "agustos": 8, "ağustos": 8, "eylul": 9, "eylül": 9,
    "ekim": 10, "kasim": 11, "kasım": 11, "aralik": 12, "aralık": 12
}

def parse_deadline_info(text: str, source: str = "", summary: str = "") -> Dict[str, Any]:
    """
    İlan metninden ve kaynağından son başvuru tarihini ve kalan gün sayısını hesaplar.
    Resmi Gazete ve Basın İlan Kurumu gibi kaynaklarda yayım tarihine yasal 15 gün başvuru süresi ekler.
    Tarih aralıklarında (örn. 08 Eylül - 22 Eylül) bitiş tarihini esas alır.
    Dönüş: { deadline_iso, days_left, badge_text, badge_type }
    """
    combined_text = f"{text or ''} {summary or ''}".strip()
    if not combined_text:
        return {"deadline_iso": None, "days_left": None, "badge_text": "", "badge_type": "default"}

    text_lower = combined_text.lower()
    today = date.today()
    current_year = today.year
    parsed_date = None

    # Sayısal tarihler (DD.MM.YYYY veya DD/MM/YYYY)
    num_matches = re.findall(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", combined_text)
    
    # Türkçe ay isimli tarihler (ör: "22 Eylül 2026" veya "22 Eylül")
    month_matches = re.findall(r"(\d{1,2})\s+([a-zçğıöşü]+)(?:\s+(\d{4}))?", text_lower)
    valid_month_matches = [m for m in month_matches if m[1] in TR_MONTHS]

    # Kaynağa göre değerlendirme:
    src_clean = (source or "").lower()
    is_resmi_gazete = "resmi gazete" in src_clean
    is_bik = "basın ilan" in src_clean or "ilan.gov.tr" in src_clean
    is_msb = "savunma" in src_clean or "msb" in src_clean
    is_iskur = "işkur" in src_clean
    is_memurlar = "memurlar" in src_clean
    is_sbb = "kamu ilan" in src_clean or "sbb" in src_clean

    # Eğer metinde açıkça bir tarih aralığı varsa ("X - Y"), her zaman son tarihi al
    if len(valid_month_matches) >= 2:
        last_m = valid_month_matches[-1]
        try:
            d = int(last_m[0])
            m = TR_MONTHS[last_m[1]]
            y = int(last_m[2]) if last_m[2] else current_year
            parsed_date = date(y, m, d)
        except Exception:
            pass
    elif len(num_matches) >= 2 and ("-" in combined_text or "/" in combined_text or "başvuru" in text_lower):
        last_num = num_matches[-1]
        try:
            d, m, y = int(last_num[0]), int(last_num[1]), int(last_num[2])
            parsed_date = date(y, m, d)
        except Exception:
            pass

    # Açık "son başvuru" ifadesi var mı?
    if not parsed_date:
        sb_match = re.search(r"son\s+başvuru(?:\s+tarihi)?\s*:?\s*(\d{1,2})[./](\d{1,2})[./](\d{4})", text_lower)
        if sb_match:
            try:
                parsed_date = date(int(sb_match.group(3)), int(sb_match.group(2)), int(sb_match.group(1)))
            except Exception:
                pass

    if not parsed_date and valid_month_matches:
        last_m = valid_month_matches[-1]
        try:
            d = int(last_m[0])
            m = TR_MONTHS[last_m[1]]
            y = int(last_m[2]) if last_m[2] else current_year
            parsed_date = date(y, m, d)
        except Exception:
            pass

    # Tekil sayısal tarih varsa:
    if not parsed_date and num_matches:
        first_num = num_matches[0]
        try:
            d, m, y = int(first_num[0]), int(first_num[1]), int(first_num[2])
            raw_d = date(y, m, d)
            
            # Resmi Gazete ve Basın İlan Kurumu'nda belirtilen tarih YAYIM tarihidir.
            # Yasal başvuru süresi Resmi Gazete/BIK yayım tarihinden itibaren en az 15 gündür.
            if is_resmi_gazete:
                parsed_date = raw_d + timedelta(days=15)
            elif is_bik:
                parsed_date = raw_d + timedelta(days=15)
            elif is_msb:
                parsed_date = raw_d + timedelta(days=30)
            elif is_iskur or is_memurlar or is_sbb or "son başvuru" in text_lower:
                # İŞKUR ve Memurlar.Net'te verilen tarih doğrudan son başvuru tarihidir
                parsed_date = raw_d
            else:
                # Diğer durumlarda eğer tarih geçmişteyse ama 15 günden yeniyse yayım tarihi kabul et
                if (raw_d - today).days < 0 and (today - raw_d).days <= 15:
                    parsed_date = raw_d + timedelta(days=15)
                else:
                    parsed_date = raw_d
        except Exception:
            pass

    if not parsed_date:
        return {"deadline_iso": None, "days_left": None, "badge_text": text[:30] if text else "", "badge_type": "default"}

    days_left = (parsed_date - today).days
    deadline_iso = parsed_date.strftime("%Y-%m-%d")

    if days_left < 0:
        badge_text = "Süresi Doldu"
        badge_type = "expired"
    elif days_left == 0:
        badge_text = "Bugün Son Gün!"
        badge_type = "urgent"
    elif days_left == 1:
        badge_text = "Yarın Son Gün!"
        badge_type = "urgent"
    elif days_left <= 3:
        badge_text = f"Son {days_left} Gün!"
        badge_type = "warning"
    elif days_left <= 7:
        badge_text = f"{days_left} Gün Kaldı"
        badge_type = "soon"
    else:
        badge_text = f"{days_left} Gün Kaldı"
        badge_type = "normal"

    return {
        "deadline_iso": deadline_iso,
        "days_left": days_left,
        "badge_text": badge_text,
        "badge_type": badge_type
    }

# 81 İl Haritası ve Normalleştirme
PROVINCES_MAP = {
    "adana": "adana", "adıyaman": "adıyaman", "afyon": "afyonkarahisar", "afyonkarahisar": "afyonkarahisar",
    "ağrı": "ağrı", "amasya": "amasya", "ankara": "ankara", "antalya": "antalya", "artvin": "artvin",
    "aydın": "aydın", "balıkesir": "balıkesir", "bilecik": "bilecik", "bingöl": "bingöl", "bitlis": "bitlis",
    "bolu": "bolu", "burdur": "burdur", "bursa": "bursa", "çanakkale": "çanakkale", "çankırı": "çankırı",
    "çorum": "çorum", "denizli": "denizli", "diyarbakır": "diyarbakır", "edirne": "edirne", "elazığ": "elazığ",
    "erzincan": "erzincan", "erzurum": "erzurum", "eskişehir": "eskişehir", "gaziantep": "gaziantep",
    "giresun": "giresun", "gümüşhane": "gümüşhane", "hakkari": "hakkari", "hatay": "hatay", "ısparta": "ısparta",
    "mersin": "mersin", "içel": "mersin", "istanbul": "istanbul", "izmir": "izmir", "kars": "kars",
    "kastamonu": "kastamonu", "kayseri": "kayseri", "kırklareli": "kırklareli", "kırşehir": "kırşehir",
    "kocaeli": "kocaeli", "konya": "konya", "kütahya": "kütahya", "malatya": "malatya", "manisa": "manisa",
    "kahramanmaraş": "kahramanmaraş", "maraş": "kahramanmaraş", "mardin": "mardin", "muğla": "muğla",
    "muş": "muş", "nevşehir": "nevşehir", "niğde": "niğde", "ordu": "ordu", "rize": "rize", "sakarya": "sakarya",
    "samsun": "samsun", "siirt": "siirt", "sinop": "sinop", "sivas": "sivas", "tekirdağ": "tekirdağ",
    "tokat": "tokat", "trabzon": "trabzon", "tunceli": "tunceli", "şanlıurfa": "şanlıurfa", "urfa": "şanlıurfa",
    "uşak": "uşak", "van": "van", "yozgat": "yozgat", "zonguldak": "zonguldak", "aksaray": "aksaray",
    "bayburt": "bayburt", "karaman": "karaman", "kırıkkale": "kırıkkale", "batman": "batman", "şırnak": "şırnak",
    "bartın": "bartın", "ardahan": "ardahan", "ığdır": "ığdır", "yalova": "yalova", "karabük": "karabük",
    "kilis": "kilis", "osmaniye": "osmaniye", "düzce": "düzce"
}

CEVRE_ILLER_KEYWORDS = [
    "ordu", "sinop", "amasya", "çorum", "corum", "tokat", "giresun", "trabzon"
]

SAMSUN_DISTRICTS = [
    "ilkadım", "ilkadim", "atakum", "canik", "tekkeköy", "tekkekoy",
    "bafra", "çarşamba", "carsamba", "terme", "havza", "vezirköprü", "vezirkopru",
    "alaçam", "alacam", "19 mayıs", "19 mayis", "kavak", "asarcık", "asarcik",
    "ladik", "salıpazarı", "salipazari", "ayvacık", "ayvacik", "yakakent"
]

SAMSUN_KEYWORDS = [
    "samsun", "ondokuz mayıs", "ondokuz mayis", "omü",
    "samsun üniversitesi", "samü"
] + SAMSUN_DISTRICTS

MAKINE_KEYWORDS = [
    "makine mühendis", "makina mühendis", "makine muhendis", "makina muhendis",
    "makine lisans", "makina lisans", "mekatronik", "enerji sistemleri",
    "otomotiv mühendis", "iklimlendirme", "tesisat", "imalat mühendis"
]

MUHENDIS_KEYWORDS = [
    "mühendis", "muhendis", "mühendisliği", "muhendisligi",
    "mühendislik", "muhendislik", "müh.", "müh "
]

KIT_KEYWORDS = [
    "tpao", "botaş", "botas", "teiaş", "teias", "eüaş", "euas", "tcdd", "mke",
    "eti maden", "dhmi", "dhmı", "türasaş", "turasas", "dsi",
    "tübitak", "tubitak", "aselsan", "roketsan", "havelsan", "tai", "tusaş", "tusas",
    "sivil havacılık", "sivil havacilik", "shgm", "epdk", "bddk", "spk",
    "rekabet kurumu", "enerji ve tabii", "mta", "tenmak"
]

BELEDIYE_KEYWORDS = [
    "belediye", "belediyesi", "büyükşehir", "buyuksehir", "özel idare", "ozel idare",
    "il özel idaresi", "il ozel idaresi", "saski", "iski", "aski", "diski", "buski",
    "koski", "gaski", "teski", "muski", "maraski", "vaski", "belediyeler birliği"
]

UNIVERSITE_KEYWORDS = [
    "üniversite", "universite", "rektörlük", "rektorluk",
    "omü", "omu", "samü", "samu"
]

KARIYER_KEYWORDS = [
    "uzman yardımc", "uzman yardimc", "müfettiş yardımc", "mufettis yardimc",
    "denetçi yardımc", "denetci yardimc", "kontrolör yardımc", "kariyer meslek",
    "stajyer kontrolör"
]

SOZLESMELI_KEYWORDS = [
    "sözleşmeli", "sozlesmeli", "4/b", "4b"
]

UYGUN_DIGER_KEYWORDS = [
    "uzman yardımc", "uzman yardimc", "müfettiş yardımc", "mufettis yardimc",
    "denetçi yardımc", "denetci yardimc", "sözleşmeli personel", "sozlesmeli personel",
    "büro personel", "buro personel", "programcı", "programci", "çözümleyici", "cozumleyici",
    "veri hazırlama", "veri hazirlama", "vhki", "v.h.k.i", "kpss p3", "kpssp3", "p3",
    "kariyer meslek", "lisans mezun", "memur al", "personel alımı", "personel alimi"
]

IRRELEVANT_EXCLUSIONS = [
    "tabip", "uzman tabip", "hemşire", "hemsire", "ebe", "veteriner",
    "ilahiyat", "imam", "müezzin", "muezzin", "vaiz", "kuran kursu", "sağlık teknikeri"
]

# --- RESMİ GAZETE PDF İÇERİK FİLTRESİ ---
# PDF ilk sayfasındaki metni tarayarak iş ilanı olmayanları eliyor.
RG_REJECT_PATTERNS = [
    r"ihalelere\s+kat[ıi]lmaktan\s+yasaklama",
    r"yasaklama\s+karar[ıi]",
    r"iptal\s*il[aâ]n[ıi]",
    r"d[uü]zeltme\s*il[aâ]n[ıi]",
    r"tarihi\s+sit",
    r"k[uü]lt[uü]r\s+varl[ıi]klar[ıi]n[ıi]\s+koruma",
    r"kamula[sş]t[ıi]rma",
    r"yetki\s+belgesi",
    r"ara[cç]\s+sat[ıi][sş]",
    r"gayrimenkul\s+sat[ıi][sş]",
    r"icra\s+dairesi",
    r"mahkemesi\s+ba[sş]kanl[ıi][gğ][ıi]",
    r"tasfiye",
    r"yap[ıi]\s+denetim\s+komisyonu",
    r"meclis\s+karar[ıi]",
    r"piyasa\s+d[uü]zenleme",
    r"elektrik\s+piyasas[ıi]",
    r"[oö][gğ]retim\s+[uü]yesi",
    r"[oö][gğ]retim\s+eleman[ıi]",
    r"[oö][gğ]retim\s+g[oö]revli",
    r"ara[sş]t[ıi]rma\s+g[oö]revli",
    r"profes[oö]r",
    r"do[cç]ent",
    r"doktor\s+[oö][gğ]retim",
    r"dr\.\s*[oö][gğ]r",
    r"akademik\s+personel",
    r"lisans[uü]st[uü]",
    r"doktora",
    r"tezli\s+y[uü]ksek\s+lisans",
    r"y[uü]ksek\s+lisans"
]

RG_ACCEPT_PATTERNS = [
    r"s[oö]zle[sş]meli\s+personel",
    r"personel\s+al[ıi]m",
    r"memur\s+al[ıi]m",
    r"i[sş][cç]i\s+al[ıi]m",
    r"uzman\s+yard[ıi]mc",
    r"m[uü]fetti[sş]\s+yard[ıi]mc",
    r"al[ıi]m\s+il[aâ]n[ıi]",
    r"al[ıi]nacakt[ıi]r",
    r"kadrolara\s+atanacak",
    r"kadrolu\s+memur",
    r"kpss",
    r"657\s*say[ıi]l[ıi]",
]

# --- TECRÜBE / DENEYİM FİLTRELEME MOTORU (YENİ MEZUN KORUMASI) ---

EXPERIENCE_PATTERNS = [
    # 1. Form ve Tablo bazlı Deneyim Süresi alanları (İŞKUR, SBB vb.)
    (r'\b(deneyim|tecr[uü]be|dene\s*[\'’`]?\s*m)\s*(s[uü]resi)?\s*[:=\-]?\s*(en\s*az|asgari|minimum)?\s*(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş]|alt[ıi]|yedi|sekiz|dokuz|on)\s*(y[ıi]l|sene|ay|11)?\b', 'Form Deneyim/Tecrübe Süresi Şartı'),
    (r'\b(hizmet|mesleki\s+[cç]al[ıi][sş]ma)\s*s[uü]resi\s*[:=\-]?\s*(en\s*az|asgari|minimum)?\s*(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş])\s*(y[ıi]l|sene|ay)\b', 'Hizmet / Çalışma Süresi Şartı'),
    (r'\b(deneyim|tecr[uü]be)\s*s[uü]resi\s*[:=\-]?\s*(en\s*az|asgari|minimum)?\s*[1-9]\d*\b', 'Deneyim Süresi Şartı'),
    (r'\b(sekt[oö]r\s+tecr[uü]besi|tecr[uü]besi\s+olan|deneyimi\s+olan|kamu\s+ve\s+[oö]zel\s+sekt[oö]r)\b', 'Sektör tecrübesi şartı'),
    (r'\b(ustal[ıi]k|kalfal[ıi]k|ehliyetli\s+ve\s+tecr[uü]beli|mesleki\s+yeterlilik|bonservis)\b', 'Mesleki yeterlilik / bonservis şartı'),

    # 2. Metin bazlı yıl/süre şartları
    (r'en\s*az\s*(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş]|alt[ıi]|yedi|sekiz|dokuz|on)\s*(y[ıi]l|sene|ay)\s*(mesleki\s+|i[sş]\s+|saha\s+|[sş]antiye\s+)?(tecr[uü]be|deneyim|hizmet|[cç]al[ıi][sş]m[ıi][sş])', 'Yıl/Süre bazlı tecrübe şartı'),
    (r'(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş])\s*(y[ıi]l|sene)\s*(mesleki\s+|i[sş]\s+|[sş]antiye\s+|saha\s+)?(tecr[uü]be|deneyim)', 'Yıl bazlı tecrübe şartı'),
    (r'\b(mesleki\s+tecr[uü]be|i[sş]\s+tecr[uü]besi|[sş]antiye\s+tecr[uü]besi|saha\s+tecr[uü]besi|mesleki\s+deneyim)\b', 'Mesleki/Saha tecrübesi şartı'),
    (r'\b([cç]al[ıi][sş]m[ıi][sş]\s+oldu[gğ]unu\s+belgelemek|[cç]al[ıi][sş]t[ıi][gğ][ıi]n[ıi]\s+belgelemek|fiilen\s+[cç]al[ıi][sş]m[ıi][sş]\s+olmak)\b', 'Çalışmış olduğunu belgeleme şartı'),
    (r'\b(tecr[uü]be\s+belgesi|deneyim\s+belgesi|[cç]al[ıi][sş]ma\s+belgesi|bonservis)\b', 'Tecrübe / Çalışma belgesi şartı'),
    (r'\balan[ıi]nda\s+en\s+az\s+(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş])\b', 'Alanda süre şartı'),
    (r'\balan[ıi]nda\s+(mesleki\s+)?(tecr[uü]be|deneyim)\s+sahibi\b', 'Alanda tecrübe sahibi olma şartı'),
    (r'\btecr[uü]beli\s+(m[uü]hendis|personel|eleman|uzman)\b', 'Tecrübeli personel alımı'),
    (r'\bk[ıi]demli\s+(uzma\w*|m[uü]hendis\w*|personel\w*|eleman\w*|yaz[ıi]l[ıi]mc[ıi]\w*|geli[sş]tirici\w*)\b', 'Kıdemli / Tecrübeli kadro'),
    (r'\btecr[uü]beye\s+sahip\s+olmak\b', 'Tecrübe şartı'),
    (r'\bdeneyime\s+sahip\s+olmak\b', 'Deneyim şartı')
]

WAIVER_PATTERNS = [
    r'tecr[uü]be\s*[sş]art[ıi]\s*aranma',
    r'deneyim\s*[sş]art[ıi]\s*aranma',
    r'tecr[uü]be\s*aranma',
    r'deneyim\s*aranma',
    r'tecr[uü]besiz',
    r'yeni\s*mezun',
    r'herhangi\s*bir\s*tecr[uü]be',
    r'deneyim\s*s[uü]resi\s*[:=\-]?\s*(yok|yoktur|aranmamaktad|aranmaz|0)\b',
    r'tecr[uü]be\s*s[uü]resi\s*[:=\-]?\s*(yok|yoktur|aranmamaktad|aranmaz|0)\b'
]

def check_experience_requirement(text: str) -> Tuple[bool, str]:
    """
    İlan metnini inceleyerek tecrübe, deneyim, SGK dökümü veya şantiye/meslek tecrübesi
    şartı aranıp aranmadığını denetler.
    Dönüş: (requires_experience: bool, note: str)
    """
    if not text:
        return False, ""
    
    t = turkish_lower(text)
    
    # 1. İstisna / Muafiyet kontrolü (Tecrübe şartı aranmamaktadır, yeni mezun vb.)
    for wp in WAIVER_PATTERNS:
        if re.search(wp, t):
            if any(w in t for w in ["aranmamaktadır", "aranmaz", "yoktur", "aranmıyor"]):
                return False, "Tecrübe şartı aranmamaktadır (Yeni mezuna uygun)"
    
    # 2. Tecrübe şartı tespiti
    for pattern, desc in EXPERIENCE_PATTERNS:
        m = re.search(pattern, t)
        if m:
            matched_str = m.group(0).strip()
            return True, f"{desc} ({matched_str})"
            
    return False, ""

def extract_municipal_cadres_from_text(text: str) -> List[Dict[str, Any]]:
    """
    5393 Sayılı Belediye Kanunu / Mahalli İdareler sözleşmeli personel ilanlarındaki
    kadro ve branş tablosunu yüksek doğrulukla çözümler.
    """
    if not text:
        return []
        
    cadre_matches = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m_unvan = re.search(r'\b(mühendis|tekniker|teknisyen|mimar|şehir\s+plancısı|avukat|ekonomist|veteriner|tabip|hemşire|zabıta|itfaiye|eğitmen|psikolog|sosyolog|biyolog|arkeolog|restoratör|programcı|çözümleyici)\b', line, re.I)
        if m_unvan:
            unvan = m_unvan.group(1).title()
            # Kadro satırının hemen çevresindeki 6 satırı al (satır bazlı kesin eşleşme)
            context = " ".join([l.strip() for l in lines[max(0, i-1):min(len(lines), i+8)]])
            
            # Branş tespiti
            branches = []
            for b in [
                "harita", "geomatik", "jeodezi", "inşaat", "makine", "makina", "mekatronik",
                "elektrik", "elektronik", "bilgisayar", "yazılım", "çevre", "ziraat", "gıda",
                "peyzaj", "avcılık", "yaban hayatı", "laboratuvar", "hemşirelik", "ebe", "hukuk", "psikoloji"
            ]:
                if b in context.lower():
                    branches.append(b.capitalize())
            
            # Öğrenim ve KPSS türü tespiti (Mevzuat standartları: Mühendis=Lisans, Tekniker=Önlisans, Teknisyen=Lise)
            unvan_l = unvan.lower()
            if unvan_l in ["mühendis", "mimar", "şehir plancısı", "avukat", "tabip", "ekonomist"]:
                deg = "Lisans (P3)"
            elif unvan_l == "tekniker":
                deg = "Ön Lisans (P93)"
            elif unvan_l == "teknisyen":
                deg = "Lise (P94)"
            elif re.search(r'\b(ön\s*lisans|on\s*lisans|p93|2\s*y[ıi]ll[ıi]k|myo)\b', context, re.I):
                deg = "Ön Lisans (P93)"
            elif re.search(r'\b(ortaöğretim|lise|p94)\b', context, re.I):
                deg = "Lise (P94)"
            elif re.search(r'(?<!ön\s)(?<!ön)\blisans\b|\bp3\b', context, re.I):
                deg = "Lisans (P3)"
            else:
                deg = "Lisans (P3)"
                
            branch_str = f" ({', '.join(branches)})" if branches else ""
            full_desc = f"{unvan}{branch_str} [{deg}]"
            # Yinelenen kadroları önle
            if not any(c["full_desc"] == full_desc for c in cadre_matches):
                cadre_matches.append({
                    "unvan": unvan,
                    "degree": deg,
                    "branches": branches,
                    "full_desc": full_desc
                })
    return cadre_matches

def evaluate_candidate_suitability(title: str, text: str = "", institution: str = "", source: str = "", is_exp: bool = False, exp_note: str = "") -> Tuple[bool, str, str]:
    """
    Aday Profili:
    - Lisans: Makine Mühendisliği (Mechanical Engineering) / Mekatronik / Otomotiv / İklimlendirme / Enerji Sistemleri
    - Sınav: 2024 KPSS P3 = 80 Puan
    - Deneyim: Yeni Mezun (0 Yıl / Deneyimsiz)
    - İkamet: Samsun & Karadeniz öncelikli + Türkiye Geneli Mühendislik
    
    Bu fonksiyon ilanın adaya UYGUN olup olmadığını, veya adayın girmesinin KESİNLİKLE İMKANSIZ olup olmadığını inceler.
    Dönüş: (is_suitable: bool, reason_code: str, reason_desc: str)
    """
    raw_combined = f"{title} {institution} {text}"
    combined = turkish_lower(raw_combined)
    norm = normalize_for_search(raw_combined)
    
    # 1. Personel alımı olmayan duyurular (Düzeltme, İptal, Faaliyet, Bilgi İstek, İhale Yasaklama, Meclis/İmar Kararları vb.)
    for non_rec in [
        r'd[uü]zeltme\s*il[aâ]n[ıi]', r'd[uü]zeltme\b.*?\bilan[ıi]', r'sehi[vw]\s*d[uü]zeltme',
        r'\bfaaliyet\s+duyurusu\b', r'\bbilgi\s+istek\b', r'\biptal\s*il[aâ]n[ıi]\b',
        r'\byasaklama\s+karar[ıi]\b', r'\bkamula[sş]t[ıi]rma\b', r'\bgaiplik\b',
        r'\bmahkeme\s+karar[ıi]\b', r'\bzayi\s+ilan[ıi]\b', r'\btasfiye\b',
        r'\bara[cç]\s+sat[ıi][sş]\b', r'\bgayrimenkul\s+sat[ıi][sş]\b', r'\bicra\s+dairesi\b',
        r'\bmeclis\w*\b.*?\bkarar\w*\b', r'\bimar\s+plan[ıi]\b', r'\bnaz[ıi]m\s+imar\b',
        r'\bparselasyon\b', r'\bask[ıi]\s+[cç][ıi]kar\w*\b', r'\bkredi\s+taleb?i\s+[cç]a[gğ]r[ıi]s[ıi]\b',
        r'\bfuarlar[ıi]\s+kat[ıi]l[ıi]m\s+deste[gğ]i\b', r'\bsonu[cç]\s*duyurusu\b',
        r'\byerle[sş]tirme\s*sonu[cç]\b', r'\bm[uü]lakat\s*sonu[cç]\b', r'\basil\s*ve\s*yedek\b',
        r'mydk', r'laboratuvar\s+izin'
    ]:
        if re.search(non_rec, norm):
            return False, "PERSONEL_ALIMI_DEGIL", "Personel alımı değildir (Düzeltme / Sonuç / İhale / İmar Kararı)"

    # Makine Mühendisliği ve bağlantılı teknik unvanlar kontrolü (Hem Türkçe hem norm ile kontrol)
    has_makine = any(k in norm for k in [
        "makine muhendisi", "makina muhendisi", "makine muhendisligi", "makina muhendisligi",
        "mekatronik", "otomotiv", "enerji sistemleri muhendisligi", "iklimlendirme", "imalat muhendisligi",
        "tesisat muhendisligi"
    ]) or any(k in combined for k in [
        "makine mühendisi", "makina mühendisi", "makine mühendisliği", "makina mühendisliği",
        "mekatronik", "otomotiv", "enerji sistemleri mühendisliği", "iklimlendirme", "imalat mühendisliği",
        "tesisat mühendisliği"
    ])

    # Beyaz yaka / A Grubu / Memur unvanları koruması (Uzman Yardımcısı, Mühendis vb.)
    has_white_collar = any(k in norm for k in [
        "uzman yardimc", "mufettis yardimc", "denetci yardimc", "kontrolor yardimc",
        "bilimsel programlar", "kariyer meslek", "sozlesmeli personel al", "memur al"
    ])

    # Çeşitli İlanlar genel başlığı taşıyan ve unvanı netleşmemiş içerikler
    if re.search(r'\b[cç]e[sş]itli\s*il[aâ]n(?:lar[ıi]?)?\b', normalize_for_search(title)) and not has_makine and not has_white_collar:
        return False, "PERSONEL_ALIMI_DEGIL", "Çeşitli İlanlar genel başlığıdır (Adaya uygun personel alımı kadrosu tespit edilemedi)"

    # 1.8 Özel Durum / Kontenjan Şartları (Engelli, Eski Hükümlü, TMYD - Genel aday başvuramaz)
    special_quotas = [
        (r'\b(engelli\s+(?:personel|i[sş][cç]i|kadro|al[ıi]m\w*)|engelli\s+raporu|engelli\s+stat[uü]s[uü]|engelli\s+kamu|ekpss)\b', "Engelli Kontenjanı"),
        (r'\b(eski\s+h[uü]k[uü]ml[uü]|tmyd|ter[oö]rle\s+m[uü]cadele\s+yaralanan)\b', "Eski Hükümlü / TMYD Kontenjanı")
    ]
    for q_pat, q_lbl in special_quotas:
        if re.search(q_pat, norm):
            return False, "OZEL_KONTENJAN", f"Özel durum/kontenjan şartı aranmaktadır ({q_lbl}). Genel aday başvuramaz."

    # 2. Vasıfsız Beden İşçiliği, Çöpçülük, Hizmetli, Güvenlik, Şoförlük Kadroları
    # (4 yıllık Lisans / Makine Mühendisi profiline KESİNLİKLE uygun değildir)
    unskilled_roles = [
        (r'\b([cç][oö]p\w*|kat[ıi]\s+at[ıi]k|geri\s+d[oö]n[uü][sş][uü]m\s+i[sş][cç]i\w*)\b', "Çöp / Katı Atık Toplama"),
        (r'\b(beden\s+i[sş][cç]isi\w*|vas[ıi]fs[ıi]z\s+i[sş][cç]i\w*|d[uü]z\s+i[sş][cç]i\w*|genel\s+beden\s+i[sş][cç]isi|hamal\w*|ayak[cç][ıi]\w*|ta[sş][ıi]y[ıi]c[ıi]\w*|y[uü]kleyici\w*|amele\b)\b', "Beden İşçiliği / Vasıfsız İşçi"),
        (r'\b(?:ge[cç]ici|s[uü]rekli|mevsimlik|\d+)\s+i[sş][cç]i\s+alacak\b', "Vasıfsız İşçi Alımı"),
        (r'\b(temizlik\s+(?:personel\w*|i[sş][cç]i\w*|g[oö]revli\w*)|temizlik\s+eleman[ıi]\w*|hizmetli\w*|odac[ıi]\w*|hademe\w*|[cç]ayc[ıi]\w*|bula[sş][ıi]k[cç][ıi]\w*|meydanc[ıi]\w*)\b', "Temizlik / Hizmetli"),
        (r'\b(otopark\s+(?:g[oö]revli\w*|personel\w*|eleman\w*)|oto\s*park\b|parkomat\w*|vale\b)\b', "Otopark / Vale Görevlisi"),
        (r'\b([sş]of[oö]r\w*|kamyon\s+[sş]of[oö]r\w*|otob[uü]s\s+[sş]of[oö]r\w*|a[gğ][ıi]r\s+vas[ıi]ta\s+[sş]of[oö]r\w*)\b', "Şoförlük Kadrosu"),
        (r'\b(g[uü]venlik\s+g[oö]revli\w*|[oö]zel\s+g[uü]venlik|silahl[ıi]\s+g[uü]venlik|silahs[ıi]z\s+g[uü]venlik|bek[cç]i\w*|koruma\s+ve\s+g[uü]venlik)\b', "Güvenlik / Bekçi"),
        (r'\b(a[sş][cç][ıi]\w*|garson\w*|komi\b|servis\s+eleman[ıi]\w*|barista\w*|f[ıi]r[ıi]nc[ıi]\w*|kasap\w*)\b', "Aşçılık / Garsonluk / Mutfak"),
        (r'\b(bah[cç][ıi]van\w*|park\s+bah[cç]e\s+i[sş][cç]i\w*|park\s+ve\s+bah[cç]eler?|[cç]im\s+bi[cç]me|a[gğ]a[cç]\s+budama)\b', "Bahçıvan / Park Bahçe"),
        (r'\b(mezarc[ıi]\w*|gassal\w*|kabir\s+kaz[ıi]c[ıi]|cenaze\s+y[ıi]kama|morg\s+g[oö]revli\w*)\b', "Mezarlık / Gassal / Cenaze Hizmetleri"),
        (r'\b(boyac[ıi]\w*|s[ıi]vac[ıi]\w*|kal[ıi]p[cç][ıi]\w*|demirci\w*|kaynak[cç][ıi]\w*|marangoz\w*|tesisat[cç][ıi]\w*|tamirci\w*|lastik[cç][ıi]\w*|y[ıi]kamac[ıi]\w*|oto\s*bak[ıi]m\s+eleman\w*)\b', "Usta / Tamirci Kadrosu"),
        (r'\b(i[sş]\s*makinesi\s+operat[oö]r\w*|kep[cç]e\s+operat[oö]r\w*|vin[cç]\s+operat[oö]r\w*|ekska?vat[oö]r\s+operat[oö]r\w*|bekoloder|greyder|dozer|kaloriferci\w*)\b', "Operatör / Kaloriferci"),
        (r'\b(sekreter\w*|yazman\w*|santral\s*memur\w*|santralci\w*)\b', "Sekreterlik Kadrosu")
    ]
    if not has_makine and not has_white_collar:
        for u_pat, u_lbl in unskilled_roles:
            if re.search(u_pat, norm):
                return False, "VASIFSIZ_BEDEN_ISCILIK", f"Vasıfsız beden işçiliği / hizmet kadrosudur ({u_lbl}). 4 yıllık lisans mezuniyeti için uygun değildir."

    # 3. Akademik Kadrolar ve Lisansüstü Şartları (Doçent, Doktora, Yüksek Lisans vb. - 4 yıllık düz lisans mezunları başvuramaz)
    for acad_pat, acad_label in [
        (r'\bö[gğ]retim\s+[uü]yesi\b', "Öğretim Üyeliği kadrosu (Prof./Doç./Dr. Öğr. Üyesi)"),
        (r'\bö[gğ]retim\s+g[oö]revli\w*\b', "Öğretim Görevlisi kadrosu (Lisansüstü/Akademik şart)"),
        (r'\bara[sş]t[ıi]rma\s+g[oö]revli\w*\b', "Araştırma Görevlisi kadrosu (Tezli Yüksek Lisans/Doktora şartı)"),
        (r'\bö[gğ]retim\s+eleman[ıi]\w*\b', "Öğretim Elemanı kadrosu"),
        (r'\bprofes[oö]r\w*\b', "Profesörlük kadrosu"),
        (r'\bdo[cç]ent\w*\b', "Doçentlik şartı"),
        (r'\bdoktor\s+[oö][gğ]retim\b', "Doktor Öğretim Üyesi kadrosu"),
        (r'\bdr\.\s*[oö][gğ]r\.\s*[uü]yesi\b', "Dr. Öğr. Üyesi kadrosu"),
        (r'\bdr\.\s*[oö][gğ]r\b', "Dr. Öğretim Üyesi kadrosu"),
        (r'\by[uü]ksek\s+lisans\s+(yapm[ıi][sş]|mezun|tamamlam[ıi][sş])\b', "Yüksek Lisans mezuniyeti şartı"),
        (r'\btezli\s+y[uü]ksek\s+lisans\b', "Tezli Yüksek Lisans şartı"),
        (r'\bdoktora\s+(yapm[ıi][sş]|mezun|derecesi|tamamlam[ıi][sş]|unvan)\b', "Doktora derecesi / mezuniyeti şartı"),
        (r'\bdoktoras[ıi]n[ıi]\s+(yapm[ıi][sş]|tamamlam[ıi][sş])\b', "Doktora şartı"),
        (r'\blisans[uü]st[uü]\s+(e[gğ]itim|mezun|yap[ıi]yor)\b', "Lisansüstü eğitim şartı"),
        (r'\bt[ıi]pta\s+uzmanl[ıi]k\b', "Tıpta Uzmanlık şartı"),
        (r'\bakademik\s+personel\b', "Akademik Personel kadrosu"),
        (r'\bales\b.*\byabanc[ıi]\s+dil\b', "Akademik kadro sınav şartı (ALES + Yabancı Dil)")
    ]:
        if re.search(acad_pat, norm):
            return False, "AKADEMIK_KADRO", f"Akademik / Lisansüstü kadrodur ({acad_label}). 4 yıllık düz lisans mezunları başvuramaz."

    # 4. Deneyim / Tecrübe Şartı (Yeni mezun başvuramaz)
    if is_exp:
        return False, "TECRUBE_ENGELI", f"Tecrübe/Deneyim şartı aranmaktadır ({exp_note})"
    
    is_txt_exp, txt_exp_note = check_experience_requirement(text)
    if is_txt_exp:
        return False, "TECRUBE_ENGELI", f"Tecrübe/Deneyim şartı aranmaktadır ({txt_exp_note})"

    # 5. İlköğretim / Ortaöğretim (Lise) / Ön Lisans Münhasır Kadroları (4 Yıllık Lisans KPSS P3 başvuramaz veya uygun değildir)
    sub_deg_pat = r'\b(ön\s*lisans|on\s*lisans|p93|orta[oö][gğ]retim|lise\b|meslek\s*lisesi|p94|2\s*y[ıi]ll[ıi]k|iki\s*y[ıi]ll[ıi]k|myo|meslek\s*y[uü]ksek\s*okul|ilk[oö][gğ]retim|ilkokul|ortaokul)\b'
    has_sub_degree = bool(re.search(sub_deg_pat, norm))
    
    # 4 yıllık Lisans (Önlisans kelimesi içindeki 'lisans' hariç tutulur!)
    lisans_pat = r'(?<!ön\s)(?<!ön)(?<!on\s)(?<!on)\b(lisans|fak[uü]lte|4\s*y[ıi]ll[ıi]k|d[oö]rt\s*y[ıi]ll[ıi]k|m[uü]hendislik|uzman)\b|\bp3\b'
    has_lisans = bool(re.search(lisans_pat, norm))
    
    if has_sub_degree and not has_lisans:
        return False, "EGITIM_DUZEYI_UYUSMAZLIGI", "İlköğretim / Ortaöğretim (Lise) veya Ön Lisans düzeyinde kadrodur (Lisans KPSS P3 başvuramaz)"

    # 6. Branş ve Diploma Uyuşmazlığı:
    incompatible_branches = [
        (r'\b(kimyager\w*|kimya\b|biyolog\w*|biyoloji\w*|fizik[cç]i\w*|matematik[cç]i\w*|istatistik[cç]i\w*)\b', "Fen / Temel Bilimler"),
        (r'\b(psikolog\w*|psikolojik\w*|fizyoterapist\w*|hem[sş]ire\w*|doktor\w*|tabip\w*|ebe\b|sa[gğ]l[ıi]k\s+tekniker\w*|odyolog\w*|diyetisyen\w*|beslenme\s+ve\s+diyetetik|eczac[ıi]\w*|laboratuvar\s+teknoloji\w*)\b', "Sağlık / Tıp / Psikoloji"),
        (r'\b(avukat\w*|hukuk\s+fak[uü]ltesi|hukuk\s+s[ıi]n[ıi]f[ıi]|h[aâ]kim\w*|savc[ıi]\w*)\b', "Hukuk Fakültesi / Avukatlık"),
        (r'\b([oö][gğ]retmen\w*|egitmen\w*|e[gğ]itmen\w*|[cç]ocuk\s+geli[sş]imi\w*|okul\s+[oö]ncesi|rehberlik\w*)\b', "Eğitim Fakültesi / Öğretmenlik"),
        (r'\b(ziraat\w*|veteriner\w*|orman\s+m[uü]hendis\w*|g[ıi]da\s+(?:m[uü]hendis\w*|teknolo\w*|teknoloji\w*))\b', "Ziraat / Veteriner / Orman / Gıda"),
        (r'\b(peyzaj\w*|avc[ıi]l[ıi]k\w*|yaban\s+hayat\w*|s[uü]s\s+bitki\w*|bah[cç]e\s+bitki\w*|bitki\s+koruma|tar[ıi]m\w*)\b', "Peyzaj / Avcılık / Ziraat / Yaban Hayatı"),
        (r'\b(harita\s+m[uü]hend\w*|jeodezi\w*|fotogrametri\w*|geomatik\w*)\b', "Harita / Geomatik Mühendisliği"),
        (r'\b(in[sş]aat\s+m[uü]hendis\w*|mimarl[ıi]k\w*|[sş]ehir\s+(?:ve\s+)?b[oö]lge\s+planlama\w*|[sş]ehir\s+planc[ıi1l]\w*|peyzaj\s+mimar\w*|i[cç]\s+mimar\w*)\b', "İnşaat / Mimarlık / Şehir ve Bölge Planlama"),
        (r'\b(elektrik\s+m[uü]hendis\w*|elektronik\s+m[uü]hendis\w*|elektrik-elektronik\w*)\b', "Elektrik-Elektronik Mühendisliği"),
        (r'\b(bilgisayar\s+m[uü]hendis\w*|yaz[ıi]l[ıi]m\s+m[uü]hendis\w*|bili[sş]im\s+personeli\w*)\b', "Bilgisayar / Yazılım"),
        (r'\b(maden\s+m[uü]hendis\w*|jeoloji\s+m[uü]hendis\w*|jeofizik\s+m[uü]hendis\w*|petrol\s+m[uü]hendis\w*)\b', "Maden / Jeoloji / Petrol"),
        (r'\b(tekstil\s+m[uü]hendis\w*|deri\s+m[uü]hendis\w*|seramik\s+m[uü]hendis\w*|metalurji\w*)\b', "Tekstil / Malzeme"),
        (r'\b([cç]evre\s+m[uü]hendis\w*|su\s+[uü]r[uü]nleri\s+m[uü]hendis\w*)\b', "Çevre / Su Ürünleri"),
        (r'\b(arkeolog\w*|m[uü]ze\s+ara[sş]t[ıi]rmac\w*|sanat\s+tarih\w*|sosyolog\w*|sosyal\s+[cç]al[ıi][sş]mac\w*|sosyal\s+hizmet\w*)\b', "Arkeoloji / Sosyal Hizmet"),
        (r'\b(pilot\w*|u[cç]u[sş]\s+tekn[ıi]ker\w*|u[cç]u[sş]\s+tab[ıi]b\w*|kab[ıi]n\s+emn[ıi]yet\w*|hava\s+traf[ıi]k\s+kontrol\w*|atsep\b)\b', "Havacılık / Uçuş Personeli"),
        (r'\b(imam\w*|m[uü]ezz[ıi]n\w*|va[ıi]z\w*|[ıi]lah[ıi]yat\w*|kuran\s+kursu\w*)\b', "Diyanet / Din Hizmetleri")
    ]
    
    if not has_makine:
        for pat, branch_name in incompatible_branches:
            if re.search(pat, norm):
                # Genel mühendislik veya genel lisans istisnası var mı?
                is_general_license = any(k in norm for k in [
                    "herhangi bir lisans", "fakultelerin lisans", "tum lisans", "4 yillik bolumlerinden",
                    "muhendislik fakultesi mezunu olmak", "muhendislik lisans"
                ])
                if not is_general_license:
                    return False, "BRANS_UYUSMAZLIGI", f"İlanda Makine Mühendisliği dışındaki bir lisans ({branch_name}) istenmektedir."

    return True, "UYGUN", "Adayın profil ve diplomasına uygundur."

def detect_job_province(title: str, institution: str = "", city_hint: str = "") -> str:
    """İlanın hangi ile ait olduğunu tam doğrulukla tespit eder."""
    t_low = turkish_lower(title)
    i_low = turkish_lower(institution)
    c_low = turkish_lower(city_hint)
    
    if c_low and c_low in PROVINCES_MAP:
        return PROVINCES_MAP[c_low]
        
    # Köşeli parantez kontrolü: [İZMİR], [ANKARA], [SAMSUN], [ORDU]
    m_bracket = re.search(r'\[\s*([a-zA-ZçğıöşüÇĞİÖŞÜ]+)\s*\]', title)
    if m_bracket:
        bracket_city = turkish_lower(m_bracket.group(1))
        if bracket_city in PROVINCES_MAP:
            return PROVINCES_MAP[bracket_city]
            
    # Kurum veya başlıkta geçen il adları
    # Öncelikle Samsun kontrolü (Üniversite ve Valilik vb.)
    if re.search(r'\b(samsun|ondokuz\s+may[ıi]s|om[uü]|sam[uü])\b', i_low) or re.search(r'\b(samsun|ondokuz\s+may[ıi]s|om[uü]|sam[uü])\b', t_low):
        return "samsun"
        
    # Samsun ilçeleri kurum veya başlıkta geçiyor mu? (Kelime sınırıyla)
    for dist in SAMSUN_DISTRICTS:
        if re.search(rf'\b{re.escape(dist)}\b', t_low) or re.search(rf'\b{re.escape(dist)}\b', i_low):
            # Kavaklıdere (Ankara) gibi semtleri hariç tut
            if dist == "kavak" and ("kavaklıdere" in t_low or "kavaklıdere" in i_low or "ankara" in t_low or "ankara" in i_low):
                continue
            return "samsun"
            
    # Diğer iller kurum veya başlıkta var mı? (Kelime sınırıyla)
    for p_key, p_val in PROVINCES_MAP.items():
        if re.search(rf'\b{re.escape(p_key)}\b', i_low) or re.search(rf'\b{re.escape(p_key)}\b', t_low):
            return p_val
            
    return ""

def build_job_item(
    title: str,
    institution: str,
    url: str,
    source: str,
    summary: str = "",
    published_date: str = "",
    extra_text: str = "",
    city: str = ""
) -> Dict[str, Any]:
    """
    Tüm scraper'lar için standartlaştırılmış ilan objesi üretir.
    Tecrübe şartı denetimi ve profil sınıflandırmasını otomatik gerçekleştirir.
    """
    full_sample = f"{title} {institution} {summary} {extra_text}"
    is_exp, exp_note = check_experience_requirement(full_sample)
    
    tags = classify_job(title=title, institution=institution, summary=summary, full_text=extra_text, city=city, source=source)
    if is_exp and "TECRUBE_SARTI" not in tags:
        tags.append("TECRUBE_SARTI")
        
    return {
        "title": title[:200],
        "institution": institution[:150],
        "url": url,
        "source": source,
        "summary": summary[:400] if summary else title[:200],
        "tags": tags,
        "published_date": published_date or datetime.now().strftime("%d.%m.%Y"),
        "requires_experience": 1 if is_exp else 0,
        "experience_note": exp_note
    }

def classify_job(
    title: str,
    institution: str = "",
    summary: str = "",
    full_text: str = "",
    city: str = "",
    source: str = ""
) -> List[str]:
    """
    İlan metnini analiz edip adayın profiline (Makine Mühendisliği lisans, KPSS P3 = 80, Samsun & Çevre)
    uygun etiketleri (tags) tam doğruluk ve bağlam kontrolüyle belirler.
    """
    t_low = turkish_lower(title)
    i_low = turkish_lower(institution)
    s_low = turkish_lower(summary)
    full_low = turkish_lower(full_text)
    
    # Başlık + Kurum + Özet birleşimi (Birincil meta alanlar)
    primary_corpus = f"{t_low} {i_low} {s_low}".strip()
    all_corpus = f"{primary_corpus} {full_low}".strip()
    all_norm = normalize_for_search(all_corpus)
    
    tags = []
    
    # 1. TECRÜBE KONTROLÜ
    is_exp, exp_note = check_experience_requirement(all_corpus)
    if is_exp:
        tags.append("TECRUBE_SARTI")
        
    # 1.5 ADAY UYGUNLUK VE DİPLOMA DENETİMİ (Makine Mühendisi - Yeni Mezun - KPSS P3=80)
    is_suitable, reason_code, reason_desc = evaluate_candidate_suitability(
        title=title,
        text=all_corpus,
        institution=institution,
        source=source,
        is_exp=is_exp,
        exp_note=exp_note
    )
    if not is_suitable:
        # Adayın profil ve diplomasıyla girmesi İMKANSIZ olan ilanlar
        tags.append("UYGUN_DEGIL")
        return tags
    detected_prov = detect_job_province(title, institution, city_hint=city)
    is_samsun = (detected_prov == "samsun")
    is_cevre_il = (detected_prov in CEVRE_ILLER_KEYWORDS)
    
    # 3. MÜHENDİSLİK / MAKİNE KONTROLÜ
    # Meslek unvanı işçilik/garson/temizlik/zabıta/güvenlik vb. ise mühendislik verme
    unskilled = any(w in primary_corpus for w in [
        "garson", "temizlik", "beden işçisi", "şoför", "sofor", "bulaşık", "çöp",
        "aşçı", "asci", "güvenlik görevlisi", "zabıta", "itfaiye eri", "park bahçe"
    ])
    
    # 3. MÜHENDİSLİK / MAKİNE KONTROLÜ (Aday Profili: Makine Mühendisliği)
    makine_patterns = [
        r'\bmakin[ae]\s+m[uü]hendis',
        r'\bm[uü]hendis\s*[\(\[-]?\s*makin[ae]',
        r'\bmakin[ae]\s*[\(\[-]?\s*m[uü]hendis',
        r'\bmakin[ae]\s+lisans',
        r'\bmekatronik',
        r'\biklimlendirme',
        r'\btesisat\s+m[uü]hendis',
        r'\bimalat\s+m[uü]hendis',
        r'\botomotiv\s+m[uü]hendis',
        r'\benerji\s+sistemleri\s+m[uü]hendis',
        r'\b[uü]retim\s+m[uü]hendis'
    ]
    is_makine = any(re.search(pat, all_norm) for pat in makine_patterns) and not (unskilled and "mühendis" not in primary_corpus)
    
    # Adayın Mezun Olmadığı / Başvuramayacağı Diğer Mühendislik Branşları
    incompatible_eng_patterns = [
        r'\b(bilgisayar|yaz[ıi]l[ıi]m)\s+m[uü]hendis',
        r'\bula[sş]t[ıi]rma\s+m[uü]hendis',
        r'\bin[sş]aat\s+m[uü]hendis',
        r'\bziraat\s+m[uü]hendis',
        r'\borman\s+m[uü]hendis',
        r'\bg[ıi]da\s+m[uü]hendis',
        r'\b[cç]evre\s+m[uü]hendis',
        r'\bharita\s+m[uü]hendis',
        r'\bgeomatik\s+m[uü]hendis',
        r'\bjeodezi\s+m[uü]hendis',
        r'\bjeoloji\s+m[uü]hendis',
        r'\bjeofizik\s+m[uü]hendis',
        r'\bmaden\s+m[uü]hendis',
        r'\bkimya\s+m[uü]hendis',
        r'\bbiyomedikal\s+m[uü]hendis',
        r'\btekstil\s+m[uü]hendis',
        r'\bsu\s+[uü]r[uü]nleri\s+m[uü]hendis',
        r'\bbal[ıi]k[cç][ıi]l[ıi]k\s+teknolojisi\s+m[uü]hendis',
        r'\bderi\s+m[uü]hendis',
        r'\bmeteoroloji\s+m[uü]hendis',
        r'\bu[cç]ak\s+m[uü]hendis',
        r'\buzay\s+m[uü]hendis',
        r'\bgenetik\s+m[uü]hendis',
        r'\bbiyom[uü]hendis',
        r'\belektrik(\s*-\s*elektronik)?\s+m[uü]hendis',
        r'\belektronik(\s+ve\s+haberle[sş]me)?\s+m[uü]hendis',
        r'\bhaberle[sş]me\s+m[uü]hendis',
        r'\bend[uü]stri\s+m[uü]hendis',
        r'\bi[sş]letme\s+m[uü]hendis',
        r'\bmetalurji\s+(ve\s+malzeme\s+)?m[uü]hendis',
        r'\bmalzeme\s+m[uü]hendis',
    ]
    has_incompatible_eng = any(re.search(pat, all_norm) for pat in incompatible_eng_patterns)
    
    muhendis_patterns = [
        r'\bm[uü]hendis\b', r'\bm[uü]hendisli[gğ]i\b', r'\bm[uü]hendislik\b', r'\bm[uü]h\.\b'
    ]
    has_generic_engineer_word = any(re.search(pat, all_norm) for pat in muhendis_patterns) and not (unskilled and "mühendis" not in primary_corpus)
    
    # Mühendislik uygunluk kararı (Aday sadece Makine Müh. mezunudur):
    if is_makine:
        is_engineering = True
    elif has_incompatible_eng and not is_makine:
        is_engineering = False
    elif has_generic_engineer_word and not has_incompatible_eng:
        is_engineering = True
    else:
        is_engineering = False
    
    # 4. KURUMLAR (Sadece kurum adı ve başlığa göre belirlenir!)
    # Üniversite: Sadece kurum veya başlıkta üniversite/rektörlük geçiyorsa (öğrenim şartındaki 'fakülteler' kelimesi değil)
    is_uni = bool(re.search(r'\b([uü]niversite|[uü]niversitesi|rekt[oö]rl[uü][kğ]|om[uü]|sam[uü])\b', f"{i_low} {t_low}"))
    
    # Belediye: Kurum veya başlıkta belediye/özel idare geçiyorsa
    belediye_pat = r'\b(belediye|belediyesi|b[uü]y[uü]k[sş]ehir|[oö]zel\s+idare|saski|iski|aski|diski|buski|koski|gaski|teski|muski|belediyeler\s+birli[gğ]i)\b'
    is_belediye = bool(re.search(belediye_pat, f"{i_low} {t_low}"))
    
    # KİT: Kurum veya başlıkta KİT geçiyorsa (Trafik kanunu değil!)
    kit_pat = r'\b(tpao|bota[sş]|teia[sş]|e[uü]a[sş]|tcdd|mke|eti\s+maden|dhmi|dhm[ıi]|t[uü]rasa[sş]|dsi|t[uü]bitak|aselsan|roketsan|havelsan|tai|tusa[sş]|shgm|epdk|bddk|spk|rekabet\s+kurumu|tenmak|mta)\b'
    is_karayollari = bool(re.search(r'\bkarayollar[ıi]\b', f"{i_low} {t_low}")) and not bool(re.search(r'karayollar[ıi]\s+trafik\s+kanun', all_corpus))
    is_kit = bool(re.search(kit_pat, f"{i_low} {t_low}")) or is_karayollari
    
    # Kariyer Meslek:
    is_kariyer = bool(re.search(r'\b(uzman\s+yard[ıi]mc\w*|m[uü]fetti[sş]\s+yard[ıi]mc\w*|denet[cç]i\s+yard[ıi]mc\w*|kontrol[oö]r\s+yard[ıi]mc\w*|stajyer\s+kontrol[oö]r|kariyer\s+meslek|ihtisas\s+memur\w*|konsolosluk\s+ve\s+ihtisas|aday\s+meslek\s+memur\w*|kalk[ıi]nma\s+ajans\w*|uzman\s+alacak|uzman\s+al[ıi]m)\b', all_norm))
    
    # 4/B Sözleşmeli:
    is_sozlesmeli = bool(re.search(r'\b(s[oö]zle[sş]meli|4/b|4b)\b', primary_corpus))
    
    # ETİKETLERİ DOLDUR
    # 1. SAMSUN_MUHENDIS & SAMSUN_HEPSI
    if is_samsun:
        tags.append("SAMSUN_HEPSI")
        if is_makine or is_engineering:
            tags.append("SAMSUN_MUHENDIS")
    elif is_cevre_il:
        tags.append("CEVRE_ILLER")
        
    # 2. MAKINE_MUHENDIS (Tek Mühendislik Kategorisi)
    if is_makine or is_engineering:
        tags.append("MAKINE_MUHENDIS")
        
    # 4. KIT_KURUMLARI
    if is_kit:
        tags.append("KIT_KURUMLARI")
        
    # 5. BELEDIYE_MAHALLI
    if is_belediye:
        tags.append("BELEDIYE_MAHALLI")
        
    # 6. UNIVERSITE_KADRO
    if is_uni:
        tags.append("UNIVERSITE_KADRO")
        
    # 7. KARIYER_MESLEK
    if is_kariyer:
        tags.append("KARIYER_MESLEK")
        
    # 8. SOZLESMELI_4B
    if is_sozlesmeli:
        tags.append("SOZLESMELI_4B")
        
    # 9. BANA_UYGUN_DIGER (KPSS P3 80 Puan Kadroları)
    uygun_pat = r'\b(uzman\s+yard[ıi]mc|s[oö]zle[sş]meli\s+personel|b[uü]ro\s+personel|programc[ıi]|vhki|v\.h\.k\.i|kpss\s*p3|p3\b|lisans\s+mezun|memur\s+al[ıi]m)\b'
    has_irrelevant = any(k in t_low for k in IRRELEVANT_EXCLUSIONS) and not is_engineering
    if re.search(uygun_pat, all_corpus) and not is_engineering and not unskilled and not has_irrelevant:
        tags.append("BANA_UYGUN_DIGER")
        
    if not tags:
        tags.append("GENEL")
        
    return tags

# --- VERİ KAZIMA MODÜLLERİ ---

def scrape_memurlar_net() -> List[Dict[str, Any]]:
    """ilan.memurlar.net sitesindeki en güncel ilanları çeker."""
    url = "https://ilan.memurlar.net/"
    jobs = []
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        html = r.content.decode("windows-1254", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        
        seen_urls = set()
        items = soup.select("a[href*='/ilan/']")
        
        for item in items:
            href = item.get("href", "").strip()
            if not href:
                continue
            if href.startswith("//"):
                full_url = "https:" + href
            elif href.startswith("/"):
                full_url = "https://ilan.memurlar.net" + href
            else:
                full_url = href
                
            raw_title = item.get_text(strip=True)
            if not raw_title or len(raw_title) < 15 or full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            
            title = raw_title
            # Yinelenen başlıkları temizle (örn. 'Akdeniz Üniv... alacakAkdeniz Üniv... alacak.')
            m_rep = re.match(r'^(.{10,}?)\s*\1[\.\s]*$', title)
            if m_rep:
                title = m_rep.group(1).strip()
            else:
                m_rep2 = re.match(r'^(.{10,}?)(?:\s*-\s*güncellendi|\s*-\s*guncellendi)?\s*\1[\.\s]*$', title, re.I)
                if m_rep2:
                    title = m_rep2.group(1).strip()

            institution = ""
            date_info = ""
            
            date_match = re.search(r"Son başvuru tarihi\s+([^\.]+)", title, re.IGNORECASE)
            if date_match:
                date_info = f"Son Başvuru: {date_match.group(1).strip()}"
                title = re.sub(r"Son başvuru tarihi\s+[^\.]+", "", title).strip()
            
            inst_words = []
            for w in title.split():
                inst_words.append(w)
                if any(term in w.lower() for term in ["üniversite", "bakanlık", "müdürlük", "belediye", "başkanlık", "ajans"]):
                    institution = " ".join(inst_words)
                    break
            if not institution and len(inst_words) >= 3:
                institution = " ".join(inst_words[:3])

            jobs.append(build_job_item(
                title=title,
                institution=institution,
                url=full_url,
                source="Memurlar.Net",
                summary=raw_title,
                published_date=date_info
            ))
            
        logger.info(f"Memurlar.net üzerinden {len(jobs)} ilan çekildi.")
    except Exception as e:
        logger.error(f"Memurlar.net kazıma hatası: {e}")
        
    return jobs

def _validate_rg_pdf(pdf_url: str) -> Tuple[bool, str]:
    """
    Resmi Gazete PDF'ini indirip içeriğini analiz eder.
    İş ilanı olmayan içerikleri (ihale yasaklama, sit alanı, kamulaştırma vb.) filtreler.
    Dönüş: (is_valid_job, extracted_text_snippet)
    """
    try:
        r = requests.get(pdf_url, headers=DEFAULT_HEADERS, verify=False, timeout=12)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            return False, ""

        reader = pypdf.PdfReader(io.BytesIO(r.content))
        if not reader.pages:
            return False, ""

        # İlk sayfanın metnini çıkar
        text = reader.pages[0].extract_text() or ""

        # Resmi Gazete üst bilgi şeridini (header) temizleyerek gövde metnini kontrol et
        cleaned_text = re.sub(r"\d{2}\.\d{2}\.\d{4}.*?çeşitli\s*ilanlar.*?(?:\d+/\d+-\d+|\b)", "", text, flags=re.IGNORECASE).strip()

        # Dijital gövde metni yetersizse (taranmış resim ilanı ise) OCR dene
        if (len(cleaned_text) < 80 or len(text.strip()) < 150) and _ocr_engine and reader.pages[0].images:
            try:
                img_data = reader.pages[0].images[0].data
                result, _ = _ocr_engine(img_data)
                if result:
                    text = " ".join([line[1] for line in result])
            except Exception:
                pass

        t_lower = turkish_lower(text)

        # 1. Olumsuz kalıpları kontrol et — iş ilanı değilse veya akademik kadro ise reddet
        for pat in RG_REJECT_PATTERNS:
            if re.search(pat, t_lower):
                return False, text[:80]

        # 2. Olumlu kalıpları kontrol et — en az birini barındırmalı
        has_accept = any(re.search(pat, t_lower) for pat in RG_ACCEPT_PATTERNS)
        if not has_accept:
            return False, text[:80]

        return True, text[:200]
    except Exception as e:
        logger.warning(f"Resmi Gazete PDF doğrulama hatası ({pdf_url}): {e}")
        return False, ""


def scrape_resmi_gazete(existing_urls: set = None) -> List[Dict[str, Any]]:
    """resmigazete.gov.tr Çeşitli İlanlar kısmını çeker ve PDF içerik filtresi uygular."""
    base_url = "https://www.resmigazete.gov.tr"
    jobs = []
    
    today = datetime.now()
    # Son 3 günü kontrol et (hafta sonu vs. için)
    dates_to_check = [today - timedelta(days=i) for i in range(3)]
    
    # Türkçe kurum eki temizleme desenleri
    suffix_patterns = [
        r"Rektörlüğünden\s*:?\s*$", r"Müdürlüğünden\s*:?\s*$",
        r"Bakanlığından\s*:?\s*$", r"Bakanlığı\s*$",
        r"Başkanlığından\s*:?\s*$", r"Komutanlığından\s*:?\s*$",
        r"Genel Müdürlüğünden\s*:?\s*$", r"Başkanlığı\s*$",
    ]
    
    for d in dates_to_check:
        date_str = d.strftime("%Y%m%d")
        year_str = d.strftime("%Y")
        month_str = d.strftime("%m")
        cesitli_url = f"{base_url}/ilanlar/eskiilanlar/{year_str}/{month_str}/{date_str}-4.htm"
        
        # Retry mekanizması (Resmi Gazete yavaş olabiliyor)
        for attempt in range(2):
            try:
                r = requests.get(cesitli_url, headers=DEFAULT_HEADERS, verify=False, timeout=12)
                if r.status_code != 200:
                    break
                    
                r.encoding = "utf-8" if "utf-8" in r.text.lower() else "windows-1254"
                soup = BeautifulSoup(r.text, "html.parser")
                
                pdf_links = []  # (p_text, pdf_url) çiftleri
                
                for p in soup.find_all("p"):
                    a = p.find("a", href=True)
                    if not a:
                        continue
                        
                    p_text = " ".join(p.get_text().split())
                    link_href = a["href"].strip()
                    
                    if link_href.startswith("http"):
                        pdf_url = link_href
                    else:
                        pdf_url = f"{base_url}/ilanlar/eskiilanlar/{year_str}/{month_str}/{link_href}"
                        
                    if len(p_text) < 10:
                        continue
                    
                    pdf_links.append((p_text, pdf_url))
                
                # Her PDF'i doğrula ve sadece iş ilanlarını ekle
                for p_text, pdf_url in pdf_links:
                    # Zaten veritabanında varsa PDF tekrar indirilmez
                    unquoted_pdf = urllib.parse.unquote(pdf_url)
                    if existing_urls and (pdf_url in existing_urls or unquoted_pdf in existing_urls):
                        continue

                    # Başlık bazlı hızlı eleme (ihale, iflas, kamulaştırma, tasfiye vs.)
                    p_low = p_text.lower()
                    if any(skip_w in p_low for skip_w in ["ihale", "iflas", "kamulaştırma", "tasfiye", "zayi", "fesih", "kayıp", "ruhsat", "bilirkişi", "ceza"]):
                        continue

                    # PDF içerik doğrulaması
                    is_valid, _ = _validate_rg_pdf(pdf_url)
                    if not is_valid:
                        logger.info(f"RG filtre: Elenen ilan → {p_text[:50]}")
                        continue
                    
                    # Title: kurum adı + "Personel/Memur Alım İlanı" (sadece zaten içermiyorsa)
                    clean_name = p_text.rstrip(": ")
                    if re.search(r'\b[cç]e[sş]itli\s*il[aâ]n(?:lar[ıi]?)?\b', clean_name, re.IGNORECASE):
                        inst_part = clean_name.split(':')[0].strip()
                        title = f"{inst_part}: Personel/Memur Alım İlanı"
                    elif "ilan" not in clean_name.lower():
                        title = f"{clean_name}: Personel/Memur Alım İlanı"
                    else:
                        title = clean_name
                    
                    # Institution: sonekleri temizle
                    institution = p_text.rstrip(": ")
                    for sp in suffix_patterns:
                        institution = re.sub(sp, "", institution, flags=re.IGNORECASE).strip()
                    
                    jobs.append(build_job_item(
                        title=title,
                        institution=institution,
                        url=pdf_url,
                        source="Resmi Gazete",
                        summary=f"Resmî Gazete Çeşitli İlanlar: {p_text} (Tarih: {d.strftime('%d.%m.%Y')})",
                        published_date=d.strftime("%d.%m.%Y")
                    ))
                break  # Başarılı ise retry'a gerek yok
            except requests.exceptions.Timeout:
                if attempt == 0:
                    logger.warning(f"Resmi Gazete zaman aşımı (deneme {attempt+1}), tekrar deneniyor: {cesitli_url}")
                    continue
                logger.warning(f"Resmi Gazete ({cesitli_url}) 2 denemede de zaman aşımına uğradı.")
            except Exception as e:
                logger.warning(f"Resmi Gazete ({cesitli_url}) çekme hatası: {e}")
                break
            
    logger.info(f"Resmi Gazete üzerinden {len(jobs)} ilan çekildi (PDF doğrulamalı).")
    return jobs

def scrape_kamuilan_sbb() -> List[Dict[str, Any]]:
    """Cumhurbaşkanlığı SBB Kamu İlan Portalı'nı (kamuilan.sbb.gov.tr) çeker."""
    url = "https://kamuilan.sbb.gov.tr/"
    jobs = []
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, verify=False, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            
            detail_links = soup.find_all("a", href=lambda h: h and "ilandetay.aspx" in h.lower())
            seen_links = set()
            
            for a in detail_links:
                href = a["href"].strip()
                full_url = f"https://kamuilan.sbb.gov.tr/{href}" if not href.startswith("http") else href
                
                if full_url in seen_links:
                    continue
                seen_links.add(full_url)
                
                raw_text = " ".join(a.get_text().split())
                if len(raw_text) < 15:
                    continue
                    
                # Düzeltme, Sehiv veya İptal duyuruları personel alımı değildir
                raw_norm = normalize_for_search(raw_text)
                if re.search(r'\b(duzeltme|sehi[vw]|iptal)\b', raw_norm):
                    continue
                    
                date_match = re.search(r"(\d{1,2}\s+[A-Za-zçğıöşüÇĞİÖŞÜ]+\s*-\s*\d{1,2}\s+[A-Za-zçğıöşüÇĞİÖŞÜ]+)", raw_text)
                date_str = ""
                title = raw_text
                
                if date_match:
                    date_str = f"Başvuru: {date_match.group(1).strip()}"
                    title = raw_text.replace(date_match.group(1), "").strip()
                    
                inst_words = []
                institution = ""
                for w in title.split():
                    inst_words.append(w)
                    if any(term in w.lower() for term in ["üniversite", "bakanlık", "müdürlük", "belediye", "başkanlık", "kurumu", "komutanlığı"]):
                        institution = " ".join(inst_words)
                        break
                if not institution:
                    institution = " ".join(inst_words[:3]) if len(inst_words) >= 3 else title[:40]

                jobs.append(build_job_item(
                    title=title,
                    institution=institution,
                    url=full_url,
                    source="Kamu İlan (SBB)",
                    summary=f"{title} | {date_str}",
                    published_date=date_str
                ))
        logger.info(f"Kamu İlan (SBB) üzerinden {len(jobs)} ilan çekildi.")
    except Exception as e:
        logger.error(f"Kamu İlan (SBB) kazıma hatası: {e}")
        
    return jobs

def scrape_basin_ilan_kurumu(max_pages: int = 2) -> List[Dict[str, Any]]:
    """Basın İlan Kurumu (ilan.gov.tr) Kamu ve Akademik Personel ilanlarını resmi API'den çeker."""
    url = "https://www.ilan.gov.tr/api/api/services/app/Ad/AdsByFilter"
    headers = {
        "Content-Type": "application/json-patch+json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Origin": "https://www.ilan.gov.tr",
        "Referer": "https://www.ilan.gov.tr/personel-alim-ilanlari"
    }
    jobs = []
    seen_urls = set()
    
    # Sayfaları tara
    skip_list = [i * 20 for i in range(max(1, max_pages))]
    for skip in skip_list:
        try:
            payload = {
                "skipCount": skip,
                "maxResultCount": 20,
                "keys": {"ats": ["5"]}  # 5 = Kamu & Akademik Personel
            }
            r = requests.post(url, json=payload, headers=headers, verify=False, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json().get("result", {})
            ads = data.get("ads", [])
            for a in ads:
                title = a.get("title", "").strip()
                inst = a.get("advertiserName", "").strip()
                city = a.get("addressCityName", "").strip()
                url_str = a.get("urlStr", "")
                slug = (a.get("slugifyTitle") or "").lower()
                ad_id = a.get("id") or ""
                
                if not title or not url_str:
                    continue
                    
                # 1. Düzeltme ilanlarını atla
                if "duzeltme" in slug or "duzeltme" in normalize_for_search(title):
                    continue
                    
                # 2. Akademik kadroları (Öğretim Üyesi, Doçent, Araştırma/Öğretim Görevlisi) atla
                if any(ak in slug for ak in ["akademik-personel", "ogretim-elemani", "ogretim-uyesi", "arastirma-gorevlisi", "docent", "profesor"]):
                    continue
                    
                full_url = f"https://www.ilan.gov.tr{url_str}" if url_str.startswith("/") else url_str
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                
                pub_raw = a.get("publishStartDate", "")
                pub_date = pub_raw[:10] if pub_raw else ""
                if pub_date:
                    try:
                        pub_date = datetime.strptime(pub_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                    except Exception:
                        pass
                        
                institution = f"{inst} ({city})" if city else inst
                summary_text = f"{title} | {institution} | {pub_date}"
                
                # Detay içeriğini hızlıca çek (Kadroları / Hizmetli / Mühendis vb. tespit için)
                extra_text = ""
                if ad_id:
                    try:
                        r_det = requests.get(
                            f"https://www.ilan.gov.tr/api/api/services/app/AdDetail/GetAdDetail?id={ad_id}",
                            headers=headers, verify=False, timeout=5
                        )
                        if r_det.status_code == 200:
                            d_res = r_det.json().get("result", {})
                            content_html = d_res.get("content", "") or d_res.get("legalBasisDetail", "")
                            if content_html:
                                c_soup = BeautifulSoup(content_html, "html.parser")
                                extra_text = " ".join(c_soup.stripped_strings)
                    except Exception:
                        pass
                        
                jobs.append(build_job_item(
                    title=title,
                    institution=institution,
                    url=full_url,
                    source="Basın İlan Kurumu",
                    summary=summary_text,
                    published_date=pub_date,
                    city=city,
                    extra_text=extra_text
                ))
        except Exception as e:
            logger.warning(f"Basın İlan Kurumu API sayfası çekilemedi (skip={skip}): {e}")
            
    logger.info(f"Basın İlan Kurumu üzerinden {len(jobs)} ilan çekildi.")
    return jobs

def scrape_savunma_msb() -> List[Dict[str, Any]]:
    """Savunma Sanayii Başkanlığı (SSB) ve MSB personel alım/sınav duyurularını çeker."""
    jobs = []
    seen_urls = set()
    
    # 1. Savunma Sanayii Başkanlığı (SSB)
    try:
        url = "https://www.ssb.gov.tr/Website/contentList.aspx?tags=Duyurular"
        r = requests.get(url, headers=DEFAULT_HEADERS, verify=False, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not (href.startswith("http") or href.startswith("/")):
                    href = "https://www.ssb.gov.tr/" + href
                text = a.get_text(separator=" ", strip=True)
                if any(w in text.lower() or w in href.lower() for w in ["personel", "alımı", "alimi", "sınav", "sinav", "mühendis", "kadro", "uzman", "duyuru"]):
                    if len(text) > 10 and not any(text == skip for skip in ["Duyurular", "Tümünü Gör"]):
                        if href in seen_urls:
                            continue
                        seen_urls.add(href)
                        date_m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
                        pub_date = date_m.group(1) if date_m else ""
                        clean_title = text.replace(pub_date, "").replace("YENİ", "").strip()
                        tags = classify_job(title=clean_title, institution="Savunma Sanayii Başkanlığı", summary=text)
                        jobs.append(build_job_item(
                            title=clean_title,
                            institution="Savunma Sanayii Başkanlığı (SSB)",
                            url=href,
                            source="Savunma Sanayii / MSB",
                            summary=f"Savunma Sanayii Başkanlığı: {clean_title}",
                            published_date=pub_date
                        ))
    except Exception as e:
        logger.warning(f"SSB duyuruları çekme hatası: {e}")
        
    # 2. MSB Personel Temin Sayfası
    try:
        url_msb = "https://personeltemin.msb.gov.tr/Anasayfa/IcerikWeb/MDS02"
        r_msb = requests.get(url_msb, headers=DEFAULT_HEADERS, verify=False, timeout=8)
        if r_msb.status_code == 200:
            soup_msb = BeautifulSoup(r_msb.content, "html.parser")
            for h in soup_msb.find_all(["h2", "h3", "h4", "a"]):
                htext = h.get_text(strip=True)
                if any(w in htext.lower() for w in ["subay temini", "astsubay temini", "devlet memurluğu temini", "uzman erbaş"]):
                    link_el = h if h.name == "a" else h.find("a")
                    link = link_el.get("href") if link_el else url_msb
                    if link.startswith("/"):
                        link = "https://personeltemin.msb.gov.tr" + link
                    if link not in seen_urls:
                        seen_urls.add(link)
                        jobs.append(build_job_item(
                            title=f"MSB {htext}",
                            institution="Milli Savunma Bakanlığı (MSB)",
                            url=link,
                            source="Savunma Sanayii / MSB",
                            summary=f"Milli Savunma Bakanlığı Personel Temin Bilgilendirmesi: {htext}",
                            published_date=datetime.now().strftime("%d.%m.%Y")
                        ))
    except Exception as e:
        logger.warning(f"MSB temin çekme hatası: {e}")
        
    logger.info(f"Savunma Sanayii / MSB üzerinden {len(jobs)} ilan çekildi.")
    return jobs

def extract_text_from_pdf_content(pdf_bytes: bytes, max_pages: int = 8, enable_ocr: bool = True) -> str:
    """
    PDF baytlarından dijital metni çıkarır.
    Eğer PDF taranmış resim (scanned image) içeriyorsa veya dijital metin yetersizse
    ve enable_ocr=True ise RapidOCR uygulayarak görsel tabloları ve metinleri eksiksiz ayrıştırır.
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        extracted_text = ""
        for page in reader.pages[:max_pages]:
            extracted_text += (page.extract_text() or "") + "\n"
            
        clean_digital = turkish_lower(extracted_text.strip())
        
        # Eğer dijital metin yeterliyse ve kadro/şart bilgisi içeriyorsa doğrudan dön
        if len(clean_digital) > 300 and any(w in clean_digital for w in ["şart", "sart", "kadro", "kpss", "başvuru", "basvuru", "mezun"]):
            # Satır sonu tire birleştirmelerini temizle (mühen-\ndis -> mühendis)
            cleaned = re.sub(r'(\w+)[\-¬]\s*\n\s*(\w+)', r'\1\2', extracted_text)
            return cleaned.strip()
            
        # Dijital metin az veya taranmış evrak: Yalnızca OCR aktif edilmişse RapidOCR ile görsel tablolardan metin çıkar
        if enable_ocr and _ocr_engine and reader.pages:
            ocr_texts = []
            for page in reader.pages[:2]:  # En fazla 2 sayfayı incele
                sorted_imgs = sorted(page.images, key=lambda img: len(img.data), reverse=True)
                for img in sorted_imgs[:2]:
                    try:
                        img_data = img.data
                        if len(img_data) < 8192:
                            continue
                        result, _ = _ocr_engine(img_data)
                        if result:
                            ocr_texts.append("\n".join([line[1] for line in result]))
                    except Exception as ocr_e:
                        logger.debug(f"OCR sayfa hatası: {ocr_e}")
            if ocr_texts:
                combined = extracted_text.strip() + "\n" + "\n".join(ocr_texts).strip()
                cleaned = re.sub(r'(\w+)[\-¬]\s*\n\s*(\w+)', r'\1\2', combined)
                return cleaned.strip()
                    
        cleaned = re.sub(r'(\w+)[\-¬]\s*\n\s*(\w+)', r'\1\2', extracted_text)
        return cleaned.strip()
    except Exception as e:
        logger.debug(f"PDF extract hatası: {e}")
        return ""

def parse_iskur_announcement(full_url: str, title: str = "", institution: str = "", row_text: str = "", enable_ocr: bool = False) -> Dict[str, Any]:
    """
    İŞKUR ilanlarına özel yapılandırılmış veri çıkarıcı ve filtreleyici.
    EK-1 Kurum Dışı formlarının PDF ve taranmış içeriklerini ayrıştırır:
    - Meslek Adı (Garson, Şoför, Temizlik, Mühendis vb.)
    - Açık İş Sayısı
    - Deneyim Süresi (Örn: '5 Yıl', 'Yoktur')
    - Tecrübe Şartı Durumu
    - Mahalli İdareler / Belediye sözleşmeli personel kadro tabloları
    """
    res = {
        "profession": "",
        "quota": "",
        "experience_str": "",
        "requires_experience": False,
        "experience_note": "",
        "full_text": "",
        "enriched_title": title
    }
    
    # PDF ise indir ve metni oku
    pdf_text = ""
    if ".pdf" in full_url.lower():
        try:
            import html
            clean_url = html.unescape(full_url)
            parts = urllib.parse.urlsplit(clean_url)
            clean_path = urllib.parse.unquote(parts.path)
            safe_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, urllib.parse.quote(clean_path), parts.query, parts.fragment))
            headers = {
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://www.iskur.gov.tr/"
            }
            r = requests.get(safe_url, headers=headers, timeout=8)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                pdf_text = extract_text_from_pdf_content(r.content, enable_ocr=enable_ocr)
                if len(pdf_text.strip()) < 40:
                    # Taranmış resim evrak (EK-1 form): 1. sayfadaki ana görsel bloğu OCR'la
                    pdf_text = extract_text_from_pdf_content(r.content, max_pages=1, enable_ocr=True)
        except Exception as e:
            logger.warning(f"İŞKUR PDF indirme/okuma hatası ({full_url}): {e}")
            
    combined_text = f"{row_text} {pdf_text}".strip()
    res["full_text"] = combined_text
    
    if pdf_text:
        # 1. Meslek Adı (EK-1 Kurum Dışı Formu)
        # OCR varyantları: Meslek Adi, Meslek Adr, Meslek Ad1, MeslekAdi, Meslek:, Meslek Ad1:
        prof_patterns = [
            r'meslek\s*ad[ıi1rIl]\s*[:=\-]?\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\s\(\)\-\/]{2,50})',
            r'(?:meslek|Meslek)\s*[:=]\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\s\(\)\-\/]{2,50})',
            r'(?:MESLEK|Meslek)\s*[:=\-]?\s*([A-ZÇĞİÖŞÜa-zçğıöşü\s\(\)\-\/]{2,50})',
            # OCR garbled: Mes1ek, Mes|ek, MesIek varyantları
            r'[Mm]es[l1|I]e[kK]\s*[Aa]d[ıi1rIl]\s*[:=\-]?\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\s\(\)\-\/]{2,50})',
        ]
        
        found_prof = ""
        for ppat in prof_patterns:
            m_prof = re.search(ppat, pdf_text, re.I)
            if m_prof:
                raw_prof = m_prof.group(1).strip()
                # Sonraki form alanı başladığında kes
                for stop in ["niteli", "açık", "acik", "faks", "başvuru", "basvuru", 
                             "sayisi", "acık", "talep", "adres", "telefon", "irtibat",
                             "deneyim", "gecici", "geçici", "daimi"]:
                    if stop in raw_prof.lower():
                        raw_prof = raw_prof.lower().split(stop)[0].strip()
                if len(raw_prof) >= 3:
                    found_prof = raw_prof.title().strip()
                    break
        
        # Fallback: TEMİZLİK, ŞoFöR, ÇÖP, GÜVENLİK gibi bilinen kalıpları metin içinde ara
        if not found_prof:
            known_professions = [
                (r'\b(?:TEMiZLiK|TEMİZLİK|temizlik)\s*(?:G[OÖ]REVLiSi|GÖREVLİSİ|PERSONEL[İI]?|ELEMANI|İŞÇİSİ)?\b', 'Temizlik Görevlisi'),
                (r'\b(?:ÇÖP|[CÇ][OÖ]P)\s*(?:PERSONEL[İI]?|İŞÇİSİ|GÖREVLİSİ)?\b', 'Çöp Personeli'),
                (r'\b(?:GÜVENLİK|GUVENLiK|G[UÜ]VENL[İI]K)\s*(?:GÖREVLİSİ|G[OÖ]REVLiSi|PERSONEL[İI]?)?\b', 'Güvenlik Görevlisi'),
                (r'\b(?:ŞOFÖR|[ŞS][OÖ]F[OÖ]R|SOFOR)\b', 'Şoför'),
                (r'\b(?:AŞÇI|A[ŞS][CÇ]I)\b', 'Aşçı'),
                (r'\b(?:GARSON)\b', 'Garson'),
                (r'\b(?:HİZMETLİ|HiZMETLi)\b', 'Hizmetli'),
                (r'\b(?:BAHÇ[İI]VAN|BAH[CÇ][İI]VAN)\b', 'Bahçıvan'),
                (r'\b(?:BEK[CÇ][İI]|BEKC[İI])\b', 'Bekçi'),
                (r'\b(?:İTFAİYE|[İI]TFAIYE)\s*(?:ERİ|ER[İI])?\b', 'İtfaiye Eri'),
                (r'\b(?:ZABITA|ZABITA)\s*(?:MEMURU)?\b', 'Zabıta Memuru'),
                (r'\b(?:BEDEN\s*İŞÇİSİ|BEDEN\s*iSCiSi)\b', 'Beden İşçisi'),
                (r'\b(?:OTOPARK|OTO\s*PARK)\s*(?:GÖREVLİSİ|G[OÖ]REVLiSi|PERSONEL[İI]?)?\b', 'Otopark Görevlisi'),
                (r'\b(?:BOYACI|SIVACI|KALIPCI|DEMİRCİ|KAYNAKCI|MARANGOZ|TAMİRCİ)\b', 'Usta'),
                (r'\b(?:İŞ\s*MAKİNE|[İI]S\s*MAK[İI]NE)\s*(?:Sİ)?\s*(?:OPERAT[ÖO]R)\b', 'İş Makinesi Operatörü'),
                (r'\b(?:KEPÇE|VINÇ|EKSKAVATÖR|DOZER|GREYDER)\s*(?:OPERAT[ÖO]R[ÜU]?)?\b', 'Operatör'),
                (r'\b(?:ENGELLİ|ENGELLi|ENGELl[İI])\s*(?:PERSONEL|İŞÇİ|GÖREVLİ|RAPORU)?\b', 'Engelli Personel'),
                (r'\b(?:ESKİ\s*HÜKÜMLÜ|ESKi\s*HUKUMLU)\b', 'Eski Hükümlü'),
            ]
            for kp, kv in known_professions:
                if re.search(kp, pdf_text, re.I):
                    found_prof = kv
                    break
        
        res["profession"] = found_prof
        if found_prof:
            if found_prof.lower() not in title.lower():
                res["enriched_title"] = f"{title} ({found_prof} Alımı)"

        # 1.5 Mahalli İdareler / Belediye Sözleşmeli Personel Tablosu (5393 Sayılı Kanun)
        if not res["profession"]:
            m_cadres = extract_municipal_cadres_from_text(pdf_text)
            if m_cadres:
                top_c = m_cadres[0]
                res["profession"] = top_c["full_desc"]
                if top_c["full_desc"].lower() not in title.lower():
                    res["enriched_title"] = f"{title} ({top_c['full_desc']} Alımı)"

        # 2. Deneyim Süresi (Örn: '5 Yıl', '10 yil', 'Yoktur' - OCR varyantları: s[uüi1]resi)
        m_exp = re.search(r'(dene\s*[\'’`]?\s*m|deneyim|tecr[uü]be|dene[yiı]m)\s*s[uüi1]resi\s*[:=\-]?\s*([^\n\r]+)', pdf_text, re.I)
        if m_exp:
            exp_line = m_exp.group(2).strip()
            clean_exp = re.split(r'(?:ba\s*vuru|ba[sş]vuru|irtibat|adres|unvan|al[ıi]nan|meslek)', exp_line, flags=re.I)[0].strip()
            res["experience_str"] = clean_exp[:30]
            
            # Muafiyet mi?
            is_waiver = clean_exp in ['-', '—', '–', '/', '0', ''] or any(w in clean_exp.lower() for w in ['yok', 'aranmamaktad', 'aranmaz', 'muaf'])
            m_num = re.search(r'(\d+|bir|iki|[uü][cç]|d[oö]rt|be[sş]|alt[ıi]|yedi|sekiz|dokuz|on)\s*(y[ıi]l|sene|ay|11)?', clean_exp, re.I)
            if m_num and not is_waiver:
                res["requires_experience"] = True
                res["experience_note"] = f"İŞKUR Formunda Deneyim Süresi Şartı: {m_num.group(0).strip()}"

    # 3. Eğer form alanında çıkmadıysa genel metin deneyim filtrelerini çalıştır
    if not res["requires_experience"]:
        is_exp, exp_note = check_experience_requirement(combined_text)
        if is_exp:
            res["requires_experience"] = True
            res["experience_note"] = exp_note
            
    return res

def scrape_iskur_kamu(existing_urls: set = None, max_cities: int = None) -> List[Dict[str, Any]]:
    """İŞKUR Kamu Memur ve Kurum Dışı Kamu İşçi Alım ilanlarını çeker."""
    jobs = []
    seen_urls = set()
    
    urls = [
        ("https://www.iskur.gov.tr/ilanlar/kamu-memur-alim-ilanlari/", "Kamu Memur"),
        ("https://www.iskur.gov.tr/ilanlar/kurumdisi-kamu-isci-alim-ilanlari/", "Kamu İşçi")
    ]
    
    priority_cities = [
        ("samsun", "tr-55"), ("ordu", "tr-52"), ("amasya", "tr-05"), ("sinop", "tr-57"),
        ("corum", "tr-19"), ("tokat", "tr-60"), ("trabzon", "tr-61"), ("ankara", "tr-06"),
        ("istanbul", "tr-34"), ("izmir", "tr-35"), ("bursa", "tr-16"), ("kocaeli", "tr-41")
    ]
    if max_cities:
        priority_cities = priority_cities[:max_cities]
    
    for base_url, cat_name in urls:
        try:
            r = requests.get(base_url, headers=DEFAULT_HEADERS, verify=False, timeout=8)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            
            active_city_ids = set()
            for g in soup.find_all("g", attrs={"data-count": True}):
                cnt = g.get("data-count", "0")
                try:
                    if int(cnt) > 0:
                        active_city_ids.add(g.get("id", "").lower())
                except Exception:
                    pass
                    
            for c_name, c_code in priority_cities:
                if c_name not in active_city_ids:
                    continue
                city_url = f"{base_url}?idId={c_code}&il={c_name}"
                try:
                    rc = requests.get(city_url, headers=DEFAULT_HEADERS, verify=False, timeout=6)
                    if rc.status_code != 200:
                        continue
                    csoup = BeautifulSoup(rc.content, "html.parser")
                    rows = csoup.find_all(class_=lambda c: c and "clickable-row" in c)
                    for row in rows:
                        file_url = row.get("data-file-url", "")
                        if file_url.startswith("/"):
                            full_url = "https://www.iskur.gov.tr" + file_url
                        else:
                            full_url = file_url or city_url
                            
                        if full_url in seen_urls:
                            continue
                        seen_urls.add(full_url)
                        
                        unquoted_url = urllib.parse.unquote(full_url)
                        if existing_urls and (full_url in existing_urls or unquoted_url in existing_urls):
                            # İlan zaten veritabanında mevcut, PDF indirip OCR çalıştırmaya gerek yok!
                            continue
                        
                        row_text = row.get_text(separator=" | ", strip=True)
                        date_m = re.search(r"(\d{2}\.\d{2}\.\d{4})", row_text)
                        deadline_str = date_m.group(1) if date_m else ""
                        
                        parts = [p.strip() for p in row_text.split("|") if p.strip() and not p.strip().startswith("(")]
                        raw_title = parts[-1] if parts else f"{c_name.capitalize()} {cat_name} Alımı"
                        inst = parts[1] if len(parts) > 2 else f"İŞKUR {c_name.capitalize()} ({cat_name})"
                        
                        base_display_title = f"[{c_name.upper()}] {raw_title}"
                        
                        # İŞKUR ilanlarına özel form ve PDF tecrübe/meslek analizi
                        iskur_info = parse_iskur_announcement(full_url, title=base_display_title, institution=inst, row_text=row_text)
                        display_title = iskur_info.get("enriched_title") or base_display_title
                        is_exp = iskur_info["requires_experience"]
                        exp_note = iskur_info["experience_note"]
                        
                        tags = classify_job(title=display_title, institution=inst, summary=row_text, full_text=iskur_info.get("full_text", ""), city=c_name, source="İŞKUR Kamu İlanları")
                        if is_exp and "TECRUBE_SARTI" not in tags:
                            tags.append("TECRUBE_SARTI")
                            
                        # Eğer meslek unvanı işçilik/garson/temizlik/şoför vb. ise mühendislik etiketlerini temizle
                        prof_l = iskur_info.get("profession", "").lower()
                        if prof_l and any(w in prof_l for w in ["garson", "temizlik", "beden", "şoför", "sofor", "bulaşık", "çöp", "aşçı", "asci", "güvenlik"]):
                            tags = [t for t in tags if t not in ["MAKINE_MUHENDIS", "SAMSUN_MUHENDIS"]]
                        
                        jobs.append({
                            "title": display_title[:200],
                            "institution": inst[:150],
                            "url": full_url,
                            "source": "İŞKUR Kamu İlanları",
                            "summary": f"{inst} - {raw_title} (Son Başvuru: {deadline_str})",
                            "tags": tags,
                            "published_date": deadline_str or datetime.now().strftime("%d.%m.%Y"),
                            "requires_experience": 1 if is_exp else 0,
                            "experience_note": exp_note
                        })
                except Exception as ce:
                    logger.warning(f"İŞKUR şehir verisi alınamadı ({c_name}): {ce}")
        except Exception as e:
            logger.warning(f"İŞKUR ana sayfa çekilemedi ({base_url}): {e}")
            
    logger.info(f"İŞKUR Kamu İlanları üzerinden {len(jobs)} ilan çekildi.")
    return jobs

def fetch_and_analyze_job_detail(url: str, source: str, title: str, institution: str, tags: list = None) -> Dict[str, Any]:
    """
    İlanın detay sayfasını çeker, resmi kadro tablosu görselini,
    alınan pozisyonları, kontenjan sayısını, istihdam türünü ve
    80 KPSS P3 Makine Mühendisi için aday uygunluk analizini üretir.
    """
    tags = tags or []
    corpus = turkish_lower(f"{title} {institution}")
    
    # Kontenjan / Sayı tespiti (Örn: 14 sözleşmeli personel, 93 kişi, 3 mühendis)
    m_quota = re.search(
        r'(\d+)\s+(?:[a-zA-ZçğıöşüÇĞİÖŞÜ\s]{0,25}?)?\s*(sözleşmeli|personel|mühendis|memur|kişi|uzman|kadro|zabıta|tekniker)', 
        title, 
        re.IGNORECASE
    )
    quota = f"{m_quota.group(1)} Kontenjan / Personel Alımı" if m_quota else "Kontenjan Belirtilmedi"
    
    # Detay Sayfasını Çekme
    paragraphs = []
    table_images = []
    cadres = []
    full_text = ""
    
    if "memurlar.net" in url.lower():
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=8)
            html = r.content.decode("windows-1254", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            for s in soup(["script", "style", "nav", "header", "footer"]):
                s.decompose()
                
            # Tablo görselleri (Resmi Gazete / İlan ilan tabloları / Belgeler)
            doc_images = []
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "common/job/advert/documents" in src:
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = "https://ilan.memurlar.net" + src
                    if src not in doc_images:
                        doc_images.append(src)
                elif any(w in src for w in ["common/job/images", "upload", "ilan"]):
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = "https://ilan.memurlar.net" + src
                    if src not in table_images:
                        table_images.append(src)
            # Taranmış döküman/ilan formu görselleri öncelikli olsun
            table_images = doc_images + table_images
                        
            # div.detail veya alternatif içerik kapsayıcıları
            detail_div = soup.find("div", class_="detail") or soup.find("div", class_="content") or soup.find("article") or soup.find("div", id="content")
            if detail_div:
                lines = [l.strip() for l in detail_div.get_text("\n", strip=True).split("\n") if len(l.strip()) > 20]
                for l in lines:
                    if not any(skip in l.lower() for skip in ["çerez", "cookie", "telif hakkı", "kaynak :", "ilan giriş :"]):
                        if l not in paragraphs:
                            paragraphs.append(l)
                full_text = " ".join(paragraphs)
                
            # Eğer metin çok azsa ve resmi kadro tablosu görseli varsa, RapidOCR ile görselden metni oku
            if len(full_text.strip()) < 150 and table_images and _ocr_engine:
                try:
                    for t_img in table_images[:2]:
                        img_resp = requests.get(t_img, headers=DEFAULT_HEADERS, timeout=8)
                        if img_resp.status_code == 200:
                            ocr_res, _ = _ocr_engine(img_resp.content)
                            if ocr_res:
                                ocr_text = "\n".join([line[1] for line in ocr_res])
                                paragraphs.append(f"Kadro Tablosu Metni: {ocr_text[:300]}")
                                full_text += " " + ocr_text
                except Exception as img_ocr_e:
                    logger.debug(f"Memurlar tablo görseli OCR okuma hatası: {img_ocr_e}")
        except Exception as e:
            logger.warning(f"Memurlar.net detay çekme hatası ({url}): {e}")
            
    elif "resmigazete.gov.tr" in url.lower():
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, verify=False, timeout=8)
            soup = BeautifulSoup(r.text, "html.parser")
            for s in soup(["script", "style", "nav", "header", "footer"]):
                s.decompose()
            for p in soup.find_all(["p", "div"]):
                t = p.get_text(strip=True)
                if 40 < len(t) < 400 and t not in paragraphs:
                    paragraphs.append(t)
            full_text = " ".join(paragraphs)
        except Exception as e:
            logger.warning(f"Resmi Gazete detay çekme hatası ({url}): {e}")
            
    elif "ilan.gov.tr" in url.lower():
        try:
            m_id = re.search(r'/ilan/(\d+)', url)
            if m_id:
                ad_id = m_id.group(1)
                r_det = requests.get(f"https://www.ilan.gov.tr/api/api/services/app/AdDetail/GetAdDetail?id={ad_id}", headers=DEFAULT_HEADERS, verify=False, timeout=6)
                if r_det.status_code == 200:
                    d_res = r_det.json().get("result", {})
                    content_html = d_res.get("content", "") or d_res.get("legalBasisDetail", "")
                    if content_html:
                        c_soup = BeautifulSoup(content_html, "html.parser")
                        for p in c_soup.find_all(["p", "div", "li"]):
                            txt = p.get_text(strip=True)
                            if 30 < len(txt) < 500 and txt not in paragraphs:
                                paragraphs.append(txt)
                        full_text = " ".join(paragraphs)
        except Exception as e:
            logger.warning(f"BİK detay hatası: {e}")
            
    elif "ssb.gov.tr" in url.lower():
        try:
            r_ssb = requests.get(url, headers=DEFAULT_HEADERS, verify=False, timeout=6)
            if r_ssb.status_code == 200:
                s_soup = BeautifulSoup(r_ssb.content, "html.parser")
                for p in s_soup.find_all(["p", "div"]):
                    txt = p.get_text(strip=True)
                    if 40 < len(txt) < 500 and txt not in paragraphs:
                        paragraphs.append(txt)
                full_text = " ".join(paragraphs)
        except Exception as e:
            logger.warning(f"SSB detay hatası: {e}")
            
    elif "kamuilan.sbb.gov.tr" in url.lower():
        try:
            r_sbb = requests.get(
                url,
                headers={
                    "User-Agent": DEFAULT_HEADERS["User-Agent"],
                    "Referer": "https://kamuilan.sbb.gov.tr/",
                    "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8"
                },
                verify=False,
                timeout=12
            )
            if r_sbb.status_code == 200 and r_sbb.content.startswith(b"%PDF"):
                extracted_txt = extract_text_from_pdf_content(r_sbb.content)
                if extracted_txt:
                    for line in extracted_txt.split("\n"):
                        line_s = line.strip()
                        if 30 < len(line_s) < 300:
                            if line_s not in paragraphs:
                                paragraphs.append(line_s)
                    full_text = " ".join(paragraphs[:15])
        except Exception as e:
            logger.warning(f"Kamu İlan (SBB) PDF detay okuma hatası ({url}): {e}")

    elif "iskur.gov.tr" in url.lower():
        try:
            iskur_data = parse_iskur_announcement(url, title=title, institution=institution)
            if iskur_data.get("profession"):
                cadres.append(f"📋 {iskur_data['profession']}")
            if iskur_data.get("requires_experience"):
                experience_note = iskur_data.get("experience_note", "Tecrübe şartı aranmaktadır.")
                requires_experience = True
            paragraphs.append(f"İŞKUR İlanı: {title}")
            if iskur_data.get("profession"):
                paragraphs.append(f"Talep Edilen Meslek / Unvan: {iskur_data['profession']}")
            if iskur_data.get("experience_str"):
                paragraphs.append(f"Formda Belirtilen Deneyim Süresi: {iskur_data['experience_str']}")
            if iskur_data.get("full_text"):
                for line in iskur_data["full_text"].split("\n"):
                    l_str = line.strip()
                    if 25 < len(l_str) < 300 and l_str not in paragraphs:
                        paragraphs.append(l_str)
            full_text = " ".join(paragraphs[:15])
        except Exception as e:
            logger.warning(f"İŞKUR detay analiz hatası ({url}): {e}")
            
    search_text = turkish_lower(f"{corpus} {full_text}")
    
    # 1. Alınan Pozisyonlar ve Meslek Alanları
    makine_patterns = [
        r'\bmakin[ae]\s+m[uü]hendis',
        r'\bm[uü]hendis\s*[\(\[-]?\s*makin[ae]',
        r'\bmakin[ae]\s*[\(\[-]?\s*m[uü]hendis',
        r'\bmakin[ae]\s+lisans',
        r'\bmekatronik',
        r'\benerji\s+sistemleri'
    ]
    has_makine = any(bool(re.search(pat, search_text)) for pat in makine_patterns)
    if has_makine:
        cadres.append("⚙️ Makine Mühendisi")
        
    other_eng_branches = []
    if any(bool(re.search(pat, search_text)) for pat in [r'\bg[ıi]da\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*g[ıi]da']):
        cadres.append("🌾 Gıda Mühendisi")
        other_eng_branches.append("Gıda Mühendisliği")
    if any(bool(re.search(pat, search_text)) for pat in [r'\bziraat\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*ziraat']):
        cadres.append("🌱 Ziraat Mühendisi")
        other_eng_branches.append("Ziraat Mühendisliği")
    if any(bool(re.search(pat, search_text)) for pat in [r'\bin[sş]aat\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*in[sş]aat']):
        cadres.append("🏗️ İnşaat Mühendisi")
        other_eng_branches.append("İnşaat Mühendisliği")
    if any(bool(re.search(pat, search_text)) for pat in [r'\belektrik(\s*-\s*elektronik)?\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*elektrik']):
        cadres.append("⚡ Elektrik-Elektronik Mühendisi")
        other_eng_branches.append("Elektrik-Elektronik")
    if any(bool(re.search(pat, search_text)) for pat in [r'\b(bilgisayar|yaz[ıi]l[ıi]m)\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*(bilgisayar|yaz[ıi]l[ıi]m)']):
        cadres.append("💻 Bilgisayar / Yazılım Mühendisi")
        other_eng_branches.append("Bilgisayar/Yazılım")
    if any(bool(re.search(pat, search_text)) for pat in [r'\b[cç]evre\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*[cç]evre']):
        cadres.append("🌍 Çevre Mühendisi")
        other_eng_branches.append("Çevre Mühendisliği")
    if any(bool(re.search(pat, search_text)) for pat in [r'\b(harita|geomatik)\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*(harita|geomatik)']):
        cadres.append("🗺️ Harita / Geomatik Mühendisi")
        other_eng_branches.append("Harita/Geomatik")
    if any(bool(re.search(pat, search_text)) for pat in [r'\b(end[uü]stri|i[sş]letme)\s+m[uü]hendis', r'\bm[uü]hendis\s*[\(\[-]?\s*(end[uü]stri|i[sş]letme)']):
        cadres.append("📊 Endüstri Mühendisi")
        other_eng_branches.append("Endüstri Mühendisliği")
        
    if any(k in search_text for k in ["mühendis", "mühendisliği"]) and not has_makine and not other_eng_branches:
        cadres.append("🛠️ Mühendislik Kadroları")
        
    if any(k in search_text for k in ["uzman yardımc", "uzman yardimc"]):
        cadres.append("💼 Uzman Yardımcısı (A Grubu)")
    if any(k in search_text for k in ["müfettiş yardımc", "denetçi yardımc"]):
        cadres.append("💼 Müfettiş / Denetçi Yardımcısı")
    if any(k in search_text for k in ["büro personel", "buro personel", "vhki", "v.h.k.i", "bilgisayar işletmen"]):
        cadres.append("📋 Büro Personeli / Memur")
    if any(k in search_text for k in ["programcı", "çözümleyici", "bilişim"]):
        cadres.append("💻 Programcı / Bilişim Personeli")
    if any(k in search_text for k in ["tekniker", "teknisyen"]):
        cadres.append("🔧 Tekniker / Teknisyen")
    if any(k in search_text for k in ["zabıta", "zabita"]):
        cadres.append("🏛️ Zabıta Memuru")
    if any(k in search_text for k in ["hava trafik", "pilot", "kabin emniyet", "atsep"]):
        cadres.append("✈️ Havacılık / Uçuş / Hava Trafik")
    if any(k in search_text for k in ["hemşire", "hemsire", "sağlık tekniker", "ebe", "fizyoterapist"]):
        cadres.append("🏥 Sağlık Personeli")
    if any(k in search_text for k in ["koruma", "güvenlik", "guvenlik"]):
        cadres.append("🛡️ Güvenlik Görevlisi")
    if any(k in search_text for k in ["şoför", "sofor", "destek personel", "temizlik"]):
        cadres.append("🚗 Destek Personeli / Şoför")
        
    if not cadres:
        cadres.append("📋 İlanda Belirtilen Pozisyonlar")
        
    # 2. İstihdam Türü / Statü
    if any(k in search_text for k in ["uzman yardımc", "uzman yardimc", "müfettiş yardımc", "denetçi yardımc"]):
        emp_type = "A Grubu Kariyer Meslek (Devlet Memuru)"
    elif any(k in search_text for k in ["4/b", "4b", "sözleşmeli", "sozlesmeli"]):
        emp_type = "657 Sayılı Kanun 4/B Sözleşmeli Personel"
    elif re.search(r'\b(kit|399)\b', search_text):
        emp_type = "399 Sayılı KHK Sözleşmeli KİT Personeli"
    elif any(k in search_text for k in ["belediye", "belediyesi", "özel idare"]):
        emp_type = "Mahalli İdareler / Belediye Kadrosu"
    else:
        emp_type = "Kamu Personeli Alımı (657 Sayılı Kanun)"
        
    # 3. Değerlendirme & Seçim Usulü
    if any(k in search_text for k in ["yazılı ve sözlü", "yazili ve sozlu", "sözlü sınav", "mülakat", "yarışma sınavı"]):
        evaluation_method = "Yazılı / Sözlü Sınav veya Mülakat Usulü"
    elif any(k in search_text for k in ["4/b", "4b", "sözleşmeli"]):
        evaluation_method = "Mülakatsız Doğrudan KPSS P3 Puan Sıralaması"
    else:
        evaluation_method = "KPSS Puan Üstünlüğü ile Doğrudan Yerleştirme"
        
    # 3.5 Tecrübe / Deneyim Denetimi (Yeni Mezun Kontrolü)
    is_exp, exp_note = check_experience_requirement(f"{corpus} {full_text}")
    is_fresh_grad = not is_exp
    
    # 4. Aday Uygunluk Analizi (Makine Mühendisliği & KPSS P3 = 80 - Yeni Mezun)
    is_suitable, reason_code, reason_desc = evaluate_candidate_suitability(
        title=title,
        text=f"{corpus} {full_text}",
        institution=institution,
        source=source,
        is_exp=is_exp,
        exp_note=exp_note
    )
    
    if not is_suitable:
        candidate_advice = f"❌ ADAYA UYGUN DEĞİL: {reason_desc}"
        advice_badge = "target_incompatible"
    elif is_exp:
        candidate_advice = f"❌ YENİ MEZUNA UYGUN DEĞİL: İlan şartlarında tecrübe / deneyim şartı aranmaktadır ({exp_note}). Yeni mezun profiline uygun değildir."
        advice_badge = "target_experience_required"
    elif "SAMSUN_MUHENDIS" in tags:
        candidate_advice = "🏆 1. DERECE HEDEF KADRO: Samsun'da doğrudan Makine/Mühendislik alımıdır. Tecrübe şartı aranmamaktadır (Yeni mezuna uygun), KPSS P3 80 puanınızla bu ilana mutlaka başvurmalısınız!"
        advice_badge = "target_prime"
    elif has_makine or "MAKINE_MUHENDIS" in tags:
        candidate_advice = "⚙️ DİPLOMA BİREBİR EŞLEŞMESİ: İlanda doğrudan Makine Mühendisliği / Mekatronik lisans diploması aranmaktadır. Tecrübe şartı aranmamaktadır; 80 puanınız güçlü bir puandır."
        advice_badge = "target_match"
    elif other_eng_branches and not has_makine:
        b_text = ", ".join(other_eng_branches[:3])
        candidate_advice = f"⚠️ BRANŞ FARKLILIĞI: İlanda Makine Mühendisliği yerine başka mühendislik lisansı ({b_text}) istenmektedir. Makine kadrosu veya genel lisans alımı olup olmadığını tablodan kontrol ediniz."
        advice_badge = "target_branch_diff"
    elif "KIT_KURUMLARI" in tags:
        candidate_advice = "🏢 KİT / YÜKSEK MAAŞ: KİT statüsünde (399 KHK veya sözleşmeli). Tecrübe şartı aranmamaktadır (Yeni mezuna uygun). Yüksek maaş ve ikramiyeli teknik kamu pozisyonudur."
        advice_badge = "target_kit"
    elif "KARIYER_MESLEK" in tags:
        candidate_advice = "💼 A GRUBU KARİYER MESLEK: Uzman/Müfettiş yardımcılığı kadrosudur. Tecrübe aranmaz (Yeni mezun kadrosudur). 80 KPSS P3 puanınız taban puanı için yeterlidir."
        advice_badge = "target_career"
    elif "SOZLESMELI_4B" in tags:
        candidate_advice = "📜 4/B SÖZLEŞMELİ FIRSAT: Mülakatsız, doğrudan KPSS puan sıralamasıyla atama yapılır. Tecrübe şartı aranmamaktadır, 80 puanınızla başvuru yapabilirsiniz."
        advice_badge = "target_sozlesmeli"
    else:
        candidate_advice = "📋 GENEL LİSANS ALIMI: Tecrübe şartı aranmamaktadır (Yeni mezuna açık). 80 KPSS P3 puanınızla başvuru şansınızı değerlendirebilirsiniz."
        advice_badge = "target_general"
        
    return {
        "title": title,
        "institution": institution,
        "source": source,
        "url": url,
        "quota": quota,
        "cadres": cadres,
        "employment_type": emp_type,
        "evaluation_method": evaluation_method,
        "kpss_requirement": "KPSS P3 (Lisans)" if not any("p93" in c.lower() for c in cadres) else "KPSS P93 (Ön Lisans)",
        "candidate_advice": candidate_advice,
        "advice_badge": advice_badge,
        "is_suitable": is_suitable,
        "unsuitable_reason": reason_desc if not is_suitable else "",
        "table_images": table_images,
        "paragraphs": paragraphs[:15],
        "summary": " • ".join(paragraphs[:3]) if paragraphs else title,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "is_fresh_grad": is_fresh_grad,
        "experience_note": exp_note if is_exp else "Tecrübe şartı aranmamaktadır (Yeni mezunlar başvurabilir, staj yeterlidir)."
    }

def run_all_scrapers(existing_urls: set = None) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Tüm kaynakları tarar, mükerrerleri ayıklar ve birleşik ilan listesini döner."""
    if existing_urls is None:
        try:
            import database as db
            existing_urls = db.get_existing_urls()
        except Exception:
            existing_urls = set()

    all_jobs = []
    source_counts = {}
    
    # 1. Memurlar.Net
    memurlar_jobs = scrape_memurlar_net()
    source_counts["Memurlar.Net"] = len(memurlar_jobs)
    all_jobs.extend(memurlar_jobs)
    
    # 2. Kamu İlan (SBB)
    sbb_jobs = scrape_kamuilan_sbb()
    source_counts["Kamu İlan (SBB)"] = len(sbb_jobs)
    all_jobs.extend(sbb_jobs)
    
    # 3. Resmi Gazete (Önceden tarananları atlayarak hızlandırır)
    rg_jobs = scrape_resmi_gazete(existing_urls=existing_urls)
    source_counts["Resmi Gazete"] = len(rg_jobs)
    all_jobs.extend(rg_jobs)
    
    # 4. Basın İlan Kurumu (ilan.gov.tr)
    bik_jobs = scrape_basin_ilan_kurumu()
    source_counts["Basın İlan Kurumu"] = len(bik_jobs)
    all_jobs.extend(bik_jobs)
    
    # 5. Savunma Sanayii / MSB
    savunma_jobs = scrape_savunma_msb()
    source_counts["Savunma Sanayii / MSB"] = len(savunma_jobs)
    all_jobs.extend(savunma_jobs)
    
    # 6. İŞKUR Kamu İlanları (Önceden tarananları atlayarak PDF/OCR süresini sıfıra indirir)
    iskur_jobs = scrape_iskur_kamu(existing_urls=existing_urls)
    source_counts["İŞKUR Kamu İlanları"] = len(iskur_jobs)
    all_jobs.extend(iskur_jobs)
    
    # Tekilleştirme (URL bazlı)
    unique_jobs = []
    seen_urls = set()
    for j in all_jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique_jobs.append(j)
            
    logger.info(f"Toplam {len(unique_jobs)} adet tekil ilan tarandı (6 Kaynak).")
    return unique_jobs, source_counts

if __name__ == "__main__":
    print("=== Scraper Testi Başlatılıyor ===")
    jobs, counts = run_all_scrapers()
    print("Kaynak istatistikleri:", counts)
    print(f"Toplam {len(jobs)} ilan toplandı.")
