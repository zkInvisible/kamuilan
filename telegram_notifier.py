import os
import time
import requests
import html
import logging
from typing import List, Dict, Any, Tuple
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

def get_credentials() -> Tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id

def send_telegram_message(text: str, parse_mode: str = "HTML") -> bool:
    """Telegram Bot API üzerinden mesaj gönderir."""
    token, chat_id = get_credentials()
    if not token or not chat_id:
        logger.warning("Telegram Bot Token veya Chat ID tanımlanmamış. Bildirim atlanıyor.")
        return False

    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=12)
        data = r.json()
        if data.get("ok"):
            logger.info("Telegram bildirimi başarıyla iletildi.")
            return True
        else:
            logger.error(f"Telegram API hatası: {data.get('description')}")
            return False
    except Exception as e:
        logger.error(f"Telegram mesaj gönderme hatası: {e}")
        return False

def format_job_bulletin(jobs: List[Dict[str, Any]]) -> List[str]:
    """
    Yeni ilanları adayın profiline göre gruplayarak şık,
    okunabilir Telegram HTML bülten mesajlarına dönüştürür.
    Telegram 4096 karakter sınırını aşmamak için mesajları böler.
    """
    # Yeni mezun ve profil filtresi: Tecrübe şartı veya adaya uygunsuz olan ilanları eliyoruz
    raw_valid = [
        j for j in jobs
        if not (j.get("requires_experience") == 1 or "TECRUBE_SARTI" in j.get("tags", []))
        and "UYGUN_DEGIL" not in j.get("tags", [])
    ]
    if not raw_valid:
        return []

    # Çapraz kaynak tekilleştirme: Aynı ilanın farklı kaynaklardan mükerrer düşmesini engelle (Tekil gösterim)
    valid_jobs = []
    seen_sigs = set()
    for j in raw_valid:
        sig = j.get("signature") or (j.get("title", "")[:45].lower())
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        valid_jobs.append(j)

    # Kategorilere ayır
    samsun_muhendis = []
    makine_muhendis = []
    samsun_hepsi = []
    cevre_iller = []
    kit_kurumlari = []
    kariyer_meslek = []
    sozlesmeli_4b = []
    belediye_mahalli = []
    universite_kadro = []
    bana_uygun = []
    diger = []

    for j in valid_jobs:
        tags = j.get("tags", [])
        if "SAMSUN_MUHENDIS" in tags:
            samsun_muhendis.append(j)
        elif "MAKINE_MUHENDIS" in tags:
            makine_muhendis.append(j)
        elif "SAMSUN_HEPSI" in tags:
            samsun_hepsi.append(j)
        elif "CEVRE_ILLER" in tags:
            cevre_iller.append(j)
        elif "KIT_KURUMLARI" in tags:
            kit_kurumlari.append(j)
        elif "KARIYER_MESLEK" in tags:
            kariyer_meslek.append(j)
        elif "SOZLESMELI_4B" in tags:
            sozlesmeli_4b.append(j)
        elif "BELEDIYE_MAHALLI" in tags:
            belediye_mahalli.append(j)
        elif "UNIVERSITE_KADRO" in tags:
            universite_kadro.append(j)
        elif "BANA_UYGUN_DIGER" in tags:
            bana_uygun.append(j)
        else:
            diger.append(j)

    sections = []
    sections.append(f"🎓 <b>GÜNLÜK TOPLU KAMU İLAN BÜLTENİ ({len(valid_jobs)} İlan)</b>\n<i>🎯 Aday Profili: Makine Müh.| KPSS P3: 80 | Samsun & TR </i>\n")

    def render_group(header: str, group_jobs: List[Dict[str, Any]]) -> str:
        if not group_jobs:
            return ""
        lines = [f"\n<b>{header} ({len(group_jobs)})</b>"]
        for j in group_jobs[:15]:
            title_escaped = html.escape(j.get("title", "İlan"))
            inst_escaped = html.escape(j.get("institution", "") or j.get("source", ""))
            url = j.get("url", "#")
            date_info = html.escape(j.get("published_date", ""))
            
            line = f"• <b>{inst_escaped}</b>: {title_escaped}"
            if date_info:
                line += f" (<i>{date_info}</i>)"
            if "kamuilan.sbb.gov.tr" in url.lower():
                line += f"\n  🔗 <a href='https://kamuilan.sbb.gov.tr/'>SBB Kamu İlan Portalı</a> <i>(📄 PDF'i web panelinden doğrudan açabilirsiniz)</i>"
            else:
                line += f"\n  🔗 <a href='{url}'>İlan Detayına Git</a>"
            lines.append(line)
            
        if len(group_jobs) > 15:
            lines.append(f"  <i>... ve {len(group_jobs) - 15} ilan daha (Web panelinden inceleyebilirsiniz)</i>")
        return "\n".join(lines)

    if samsun_muhendis:
        sections.append(render_group("🏆 SAMSUN MÜHENDİSLİK KADROLARI (1. ÖNCELİK)", samsun_muhendis))
    if makine_muhendis:
        sections.append(render_group("⚙️ MAKİNE MÜHENDİSLİĞİ ÖZEL KADROLARI", makine_muhendis))
    if samsun_hepsi:
        sections.append(render_group("🌊 SAMSUN TÜM KADROLAR", samsun_hepsi))
    if cevre_iller:
        sections.append(render_group("🌲 ÇEVRE İLLER (Ordu, Sinop, Amasya, Çorum, Trabzon)", cevre_iller))
    if kit_kurumlari:
        sections.append(render_group("🏢 KİT & SANAYİ / TEKNİK KURUMLAR (TÜBİTAK, DSİ vb.)", kit_kurumlari))
    if kariyer_meslek:
        sections.append(render_group("💼 UZMAN YARDIMCILIĞI & KARİYER MESLEK", kariyer_meslek))
    if sozlesmeli_4b:
        sections.append(render_group("📜 657 4/B SÖZLEŞMELİ PERSONEL", sozlesmeli_4b))
    if belediye_mahalli:
        sections.append(render_group("🏛️ BELEDİYELER & MAHALLİ İDARELER", belediye_mahalli))
    if universite_kadro:
        sections.append(render_group("🎓 ÜNİVERSİTE KADROLARI", universite_kadro))
    if bana_uygun:
        sections.append(render_group("🎯 KPSS P3 / 80 PUAN UYGUN DİĞER KADROLAR", bana_uygun))

    # Footer
    sections.append("\n━━━━━━━━━━━━━━━━━━━━\n🌐 <i>Tüm ilanları web panelinden filtreleyip inceleyebilirsiniz.</i>")

    full_text = "\n".join(sections)

    # 4000 karakter sınırına göre böl
    messages = []
    if len(full_text) <= 4000:
        messages.append(full_text)
    else:
        # Uzunsa böl
        chunks = []
        curr = ""
        for line in full_text.split("\n"):
            if len(curr) + len(line) + 2 > 3800:
                chunks.append(curr)
                curr = line + "\n"
            else:
                curr += line + "\n"
        if curr:
            chunks.append(curr)
            
        for i, ch in enumerate(chunks, 1):
            messages.append(f"<b>[Günlük Bülten - Bölüm {i}/{len(chunks)}]</b>\n\n{ch}")

    return messages

def send_daily_digest(jobs: List[Dict[str, Any]]) -> Tuple[bool, int]:
    """
    Yeni ilanları derleyip Telegram'a günlük toplu bülten olarak gönderir.
    Dönüş: (success, sent_job_count)
    """
    if not jobs:
        logger.info("Bildirilecek yeni ilan bulunmuyor.")
        return True, 0

    # Yeni mezuna uygun olmayan tecrübe şartlı ilanları ele
    valid_jobs = [
        j for j in jobs
        if not (j.get("requires_experience") == 1 or "TECRUBE_SARTI" in j.get("tags", []))
    ]
    if not valid_jobs:
        logger.info("Tüm yeni ilanlarda tecrübe şartı bulunduğu için bülten atlanıyor.")
        return True, 0

    token, chat_id = get_credentials()
    if not token or not chat_id:
        logger.warning("Telegram token veya chat_id eksik olduğu için mesaj atılamadı.")
        return False, 0

    messages = format_job_bulletin(jobs)
    success = True
    for msg in messages:
        res = send_telegram_message(msg, parse_mode="HTML")
        if not res:
            success = False
        time.sleep(0.5)

    return success, len(jobs)

def send_test_message() -> Tuple[bool, str]:
    """Telegram test bildirimi kullanıcı isteği üzerine devre dışı bırakılmıştır."""
    logger.info("Test bildirimi kullanıcı tercihi doğrultusunda engellendi.")
    return False, "Test bildirimleri kapatılmıştır. Sistem ilanları günde 1 kez toplu bülten olarak iletir."

if __name__ == "__main__":
    print("=== Telegram Notifier ===")
    print("Test bildirimleri kapatıldı. Günlük bülten devrede.")
