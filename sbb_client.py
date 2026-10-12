"""Read public SBB documents using the portal's normal session and referrer."""
from urllib.parse import urlsplit

import requests

SBB_HOME = "https://kamuilan.sbb.gov.tr/"
MAX_PDF_BYTES = 16 * 1024 * 1024
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": SBB_HOME,
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
}


class SBBPDFError(ValueError):
    """The portal did not provide a valid public PDF document."""


def is_sbb_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "kamuilan.sbb.gov.tr"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def fetch_sbb_pdf(url: str) -> bytes:
    if not is_sbb_url(url):
        raise SBBPDFError("Geçerli bir SBB ilan bağlantısı gerekli.")
    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            # The portal only serves documents reached through its public homepage.
            with session.get(SBB_HOME, timeout=(5, 15)) as home:
                home.raise_for_status()
            with session.get(url, timeout=(5, 20), stream=True) as response:
                response.raise_for_status()
                if not is_sbb_url(response.url):
                    raise SBBPDFError("SBB ilanı beklenmeyen bir adrese yönlendirildi.")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    content.extend(chunk)
                    if len(content) > MAX_PDF_BYTES:
                        raise SBBPDFError("İlan belgesi görüntüleme sınırını aşıyor.")
                    if len(content) >= 5 and not content.startswith(b"%PDF-"):
                        raise SBBPDFError("SBB şu anda ilan PDF'sini sunmuyor.")
                if not content.startswith(b"%PDF-"):
                    raise SBBPDFError("SBB geçerli bir ilan PDF'si döndürmedi.")
                return bytes(content)
    except requests.RequestException as error:
        raise SBBPDFError("SBB ilan belgesine şu anda erişilemiyor.") from error
