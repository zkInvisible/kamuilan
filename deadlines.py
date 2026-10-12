"""Explicit application deadlines only: publication dates are not deadlines."""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

MONTHS = {name: number for number, name in enumerate(
    ("ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim", "kasim", "aralik"), 1)}


def local_today():
    return datetime.now(ZoneInfo("Europe/Istanbul")).date()


def fold(text):
    return str(text or "").lower().translate(str.maketrans("çğıöşü", "cgiosu")).replace("i\u0307", "i")


def dates_in(text, reference):
    found = []
    for match in re.finditer(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)", text):
        try:
            found.append((match.start(), date(int(match[1]), int(match[2]), int(match[3]))))
        except ValueError:
            pass
    for match in re.finditer(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?(?!\d)", text):
        try:
            found.append((match.start(), date(int(match[3] or reference.year), int(match[2]), int(match[1]))))
        except ValueError:
            pass
    month_pattern = "|".join(MONTHS)
    for match in re.finditer(rf"(?<!\d)(\d{{1,2}})\s+({month_pattern})(?:\s+(20\d{{2}}))?\b", text):
        try:
            found.append((match.start(), date(int(match[3] or reference.year), MONTHS[match[2]], int(match[1]))))
        except ValueError:
            pass
    # "19-23 Ekim 2026" has only the final month written out.
    for match in re.finditer(rf"(?<!\d)(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})\s+({month_pattern})(?:\s+(20\d{{2}}))?", text):
        try:
            found.append((match.start(), date(int(match[4] or reference.year), MONTHS[match[3]], int(match[1]))))
        except ValueError:
            pass
    return [value for _, value in sorted(set(found))]


def parse_deadline_info(text, source="", summary="", reference=None):
    reference = reference or local_today()
    normalized = fold(f"{text or ''} {summary or ''}")
    selected, evidence = None, ""
    contexts = []
    for match in re.finditer(r"son\s+basvuru(?:\s+tarihi|\s+gunu)?|basvuru(?:larin|lar|\s+tarihleri|\s+suresi|\s+tarihi)?|muracaat", normalized):
        context = normalized[match.start():match.end() + 230]
        values = dates_in(context, reference)
        if not values:
            continue
        explicit_end = match.group().startswith("son")
        is_period = len(values) >= 2 and bool(re.search(r"\s[-–]\s|tarihleri|arasinda|tarihine|tarihinden", context))
        # A label followed immediately by a date is also explicit.
        immediate = bool(re.match(r"\s*[:\-]?\s*\d{1,2}(?:[./]|\s+(?:" + "|".join(MONTHS) + r"))", normalized[match.end():]))
        direct_end = explicit_end and bool(re.match(r"son\s+basvuru(?:\s+tarihi|\s+gunu)?\s*[:\-]?\s*(?:en\s+gec\s+)?\d", context))
        if direct_end or is_period or immediate:
            contexts.append((2 if explicit_end else 1, values, context))
    # Date-range fields supplied by source adapters are explicit even without a label.
    if not contexts and len(normalized) < 160 and re.search(r"\s[-–]\s|\d\s*[-–]\s*\d", normalized):
        values = dates_in(normalized, reference)
        if len(values) >= 2:
            contexts.append((1, values, normalized))
    if contexts:
        _, values, evidence = max(contexts, key=lambda item: item[0])
        selected = values[-1]
        if len(values) >= 2 and values[0].month > selected.month and selected.year == values[0].year:
            selected = date(selected.year + 1, selected.month, selected.day)
    if selected is None:
        return {"deadline_iso": None, "days_left": None, "badge_text": "Son başvuru tarihi doğrulanamadı", "badge_type": "default", "confidence": "unknown"}
    days = (selected - reference).days
    badge = "Süresi Doldu" if days < 0 else "Bugün Son Gün!" if days == 0 else "Yarın Son Gün!" if days == 1 else f"{days} Gün Kaldı"
    return {"deadline_iso": selected.isoformat(), "days_left": days, "badge_text": badge,
            "badge_type": "expired" if days < 0 else "urgent" if days <= 1 else "warning" if days <= 3 else "soon" if days <= 7 else "normal",
            "confidence": "explicit", "evidence": evidence[:240]}
