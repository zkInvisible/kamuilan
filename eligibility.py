"""Small, deterministic eligibility checks; no model calls or OCR dependencies."""
import os
import re

PROFILE_SCORE = 78
PROFILE_TYPE = 3
PROFILE_KPSS_YEAR = os.environ.get("PROFILE_KPSS_YEAR", "2026")
TAG_LABELS = [
    ("MAKINE_MUHENDIS", "Makine Mühendisliği", "engineer"),
    ("BANA_UYGUN_DIGER", "Genel memurluk", ""),
    ("BELEDIYE_MAHALLI", "Belediye / mahallî idare", ""),
    ("KIT_KURUMLARI", "Kamu kurumları", ""),
    ("SOZLESMELI_4B", "4/B sözleşmeli", ""),
    ("KARIYER_MESLEK", "Kariyer kadroları", ""),
    ("UNIVERSITE_KADRO", "Üniversite idarî kadroları", ""),
]


def fold(value):
    return str(value or "").lower().translate(str.maketrans("çğıöşü", "cgiosu")).replace("i\u0307", "i")


def score_types(text):
    return {int(value) for value in re.findall(r"(?<!\w)(?:kpss\s*[-:]?\s*)?p\s*[-:]?\s*(\d{1,3})(?!\d)", fold(text))}


def minimum_scores(text):
    patterns = (
        r"(?:en\s+az|minimum|taban\s+puani?)[^.!?]{0,35}?(\d{2}(?:[.,]\d{1,2})?)(?!\d)\s*(?:puan|$)",
        r"\bp\s*3\s*[-:]?\s*(\d{2}(?:[.,]\d{1,2})?)(?!\d)",
        r"kpss(?:\s*p\s*3)?\s*(\d{2}(?:[.,]\d{1,2})?)(?!\d)",
    )
    return [float(value.replace(",", ".")) for pattern in patterns for value in re.findall(pattern, fold(text))]


def accepts_degree(text):
    return bool(re.search(
        r"makin[ae]\s+muhendis|herhangi\s+bir\s+lisans|tum\s+lisans|"
        r"lisans[^.!?]{0,180}herhangi\s+bir\s+bolum|"
        r"(?:dort|4)\s+yillik[^.!?]{0,180}herhangi\s+bir\s+bolum|\b4001\b|"
        r"muhendislik\s+fakultesi[^.!?]{0,100}(?:herhangi|tum)", fold(text),
    ))


def exclusion(text):
    """Evaluate actual vacancy/qualification cells, not criminal-law boilerplate."""
    text = fold(text)
    text = re.sub(r"(?:tecrube|deneyim|yuksek\s+lisans)(?:\s+sarti)?\s+(?:aranmamaktadir|aranmaz|aranmiyor)", "", text)
    patterns = (
        (r"ogretim\s+(?:gorevl|uye|eleman)|arastirma\s+gorevl|profesor|docent|akademik\s+personel", "Akademik kadro"),
        (r"yuksek\s+lisans|lisansustu|doktora|\bales\b", "Lisansüstü / akademik şart"),
        (r"\bekpss\b|engelli\s+(?:personel|memur|isci|kadro|alim|rapor|statu)|eski\s+hukumlu|ozel\s+kontenjan|(?:sehit|gazi)\s+(?:yakin|es|cocuk)|\btmyd\b|terorle\s+mucadele.{0,35}yaralanan", "Özel kontenjan"),
        (r"\b(?:isci\w*|tekniker\w*|teknisyen\w*|hizmetli\w*|temizlik\w*|sofor\w*|bekci\w*|guvenlik\s+gorevl\w*|subay\w*|astsubay\w*|uzman\s+erbas\w*)\b", "Hedef dışı işçilik / hizmet / askerî kadro"),
        (r"(?:en\s+az\s+)?\d+\s*(?:yil|ay)\s*(?:is\s+)?(?:tecrube|deneyim)|mesleki\s+tecrube|deneyim\s+sarti|tecrubeli", "Tecrübe şartı"),
    )
    for pattern, reason in patterns:
        if re.search(pattern, text):
            return reason
    return ""


def prefilter(title, summary=""):
    heading = fold(title)
    if exclusion(title):
        return True
    if re.search(r"\b(?:ihale|kamulastirma|mahkeme|imar|zayi|iflas|tasfiye)\b|duzeltme\s*ilani|iptal\s*ilani|sonuc\s+duyurusu|sinav\s+sonuc|yerlestirilmeye\s+hak|katilmaya\s+hak|kesin\s+kayit|erteleme\s+duyurusu|yerlestirme\s+sonucu|gorevde\s+yukselme|unvan\s+degisikligi|kurum\s+ici|naklen\s+atama", heading):
        return True
    combined = f"{title} {summary}"
    types, scores = score_types(combined), minimum_scores(combined)
    return bool((types and PROFILE_TYPE not in types) or (scores and min(scores) > PROFILE_SCORE))


def parse_layout_rows(layout):
    """Recover aligned municipal cadre tables; ambiguous cells stay unverified."""
    rows, general = [], []
    for page in layout.split("\f"):
        lines = page.splitlines()
        anchors = []
        for index, line in enumerate(lines):
            if not re.match(r"^\s*\d{1,3}\s{2,}\S", line):
                continue
            parts = list(re.finditer(r"\S+(?: \S+)*", line))
            service = next((i for i, part in enumerate(parts) if fold(part.group()) in {"gih", "th", "sh", "ah", "yh", "eh"}), None)
            code = re.search(r"\b(?:KPSS\s*)?P\s*(\d{1,3})\b", line, re.I)
            if not code or len(parts) < 4:
                continue
            if service is not None:
                if service < 2 or len(parts) <= service + 3:
                    continue
                quota_text = parts[service + 2].group()
                if not quota_text.isdigit():
                    continue
                quota = int(quota_text)
                role_end = parts[service].start()
                qualification_start = parts[service + 3].start()
            else:
                qualification = next((part for part in parts[2:] if part.start() < code.start() and len(part.group()) > 10 and not re.search(r"erkek|kadin", fold(part.group()))), None)
                if qualification is None:
                    continue
                numbers = [part for part in parts[2:] if part.start() < qualification.start() and part.group().isdigit()]
                quota_heading = re.search(r"adedi|kontenjan|sayisi", fold(" ".join(lines[max(0, index - 20):index])))
                quota = int(numbers[0].group()) if len(numbers) == 1 and quota_heading else None
                role_end = numbers[0].start() if numbers else qualification.start()
                qualification_start = qualification.start()
            gender = re.search(r"erkek|kadin|kadın", line, re.I)
            anchors.append({
                "line": index, "role_start": parts[1].start(), "role_end": role_end,
                "qualification_start": qualification_start,
                "qualification_end": gender.start() if gender else max(qualification_start + 10, code.start() - 12),
                "score_start": code.end(), "score_type": int(code.group(1)), "quota": quota,
            })
        if not anchors:
            general.extend(lines)
            continue
        role_left = min(anchor["role_start"] for anchor in anchors)
        qualification_right = min(anchor["qualification_end"] for anchor in anchors)
        for anchor in anchors:
            anchor["role_start"] = role_left
            anchor["qualification_end"] = qualification_right
        first = anchors[0]
        headings = [i for i in range(max(0, first["line"] - 25), first["line"]) if re.search(r"nitelig|\bturu\b|\bpuani\b|\badedi\b", fold(lines[i]))]
        start = headings[-1] + 1 if headings else max(0, first["line"] - 12)
        starts = [start]
        for previous, current in zip(anchors, anchors[1:]):
            candidates = []
            for index in range(previous["line"] + 1, current["line"] + 1):
                cell = lines[index][current["qualification_start"]:current["qualification_end"]].strip()
                if cell.startswith(("-", "•")) and not re.match(r"[-•]\s*(?:en\s+az|milli|millî|meb|surucu|sertifika|bilgisayar\s+sertifika)", fold(cell)):
                    candidates.append(index)
            separators = [i + 1 for i in range(previous["line"] + 1, current["line"]) if not lines[i].strip()]
            starts.append(candidates[-1] if candidates else separators[-1] if separators else (previous["line"] + current["line"] + 1) // 2)
        end = next((i for i in range(anchors[-1]["line"] + 1, len(lines)) if re.match(r"^\s*(?:\d+[.)]\s*)?(?:BASVURU|GENEL\s+SART|OZEL\s+SART|SINAV|ACIKLAMA)", fold(lines[i]).upper())), len(lines))
        bounds = starts + [end]
        for index, anchor in enumerate(anchors):
            block = lines[bounds[index]:bounds[index + 1]]
            role = " ".join(line[anchor["role_start"]:anchor["role_end"]].strip() for line in block).strip()
            qualification = " ".join(line[anchor["qualification_start"]:anchor["qualification_end"]].strip() for line in block).strip()
            score_cell = " ".join(line[anchor["score_start"]:].strip() for line in block)
            scores = re.findall(r"(?<!\d)(\d{2}(?:[.,]\d{1,2})?)(?!\d)", score_cell)
            rows.append({"title": role, "qualification": qualification, "score_type": anchor["score_type"],
                         "minimum_score": max(float(value.replace(",", ".")) for value in scores) if scores else None,
                         "quota": anchor["quota"]})
        general.extend(lines[:start])
        general.extend(lines[end:])
    return rows, "\n".join(general)


def parse_html_rows(tables):
    rows = []
    for table in tables:
        for cells in table:
            cells = [str(cell).strip() for cell in cells]
            if len(cells) < 3:
                continue
            types = score_types(" ".join(cells))
            if len(types) != 1:
                continue
            score_type = next(iter(types))
            code_index = next((i for i, cell in enumerate(cells) if score_types(cell) == {score_type}), None)
            scores = minimum_scores(" ".join(cells))
            if code_index is not None:
                scores += [float(cell.replace(",", ".")) for cell in cells[code_index + 1:] if re.fullmatch(r"\d{2}(?:[.,]\d{1,2})?", cell)]
            if not scores:
                continue
            rows.append({"title": cells[1] if cells[0].isdigit() else cells[0], "qualification": " ".join(cells),
                         "score_type": score_type, "minimum_score": max(scores), "quota": None})
    return rows


def extra_conditions(text):
    normalized = fold(text)
    conditions = []
    for label, pattern in (
        ("Yaş şartı", r"[^.\n]{0,70}\b\d{2}\s*yas[^.\n]{0,110}"),
        ("Ehliyet şartı", r"[^.\n]{0,50}(?:surucu\s+belgesi|ehliyet)[^.\n]{0,70}"),
        ("Fiziksel şart", r"[^\n]{0,70}(?:boy|kilo)[^\n]{0,130}"),
        ("Sertifika / yabancı dil", r"[^.\n]{0,50}(?:sertifika|yabanci\s+dil|yds|yokdil)[^.\n]{0,100}"),
        ("KPSS yılı", r"[^.\n]{0,60}(?:20\d{2}[^.\n]{0,70}kpss|kpss[^.\n]{0,50}20\d{2})[^.\n]{0,70}"),
    ):
        match = re.search(pattern, normalized)
        if match:
            conditions.append({"label": label, "text": re.sub(r"\s+", " ", text[match.start():match.end()]).strip()})
    return conditions


def required_kpss_years(text):
    text = re.sub(r"\b(20\d)\s+(\d)\b", r"\1\2", fold(text))
    years = set()
    for match in re.finditer(r"(20\d{2}(?:\s*(?:veya|ve|ile|,|/|-)\s*20\d{2})*)\s+yil\w*[^.!?]{0,50}(?:kpss|kamu\s+personel\w*\s+secme\s+sinav)", text):
        years.update(re.findall(r"20\d{2}", match[1]))
    for match in re.finditer(r"kpss\s*[-(]?\s*(20\d{2})\b", text):
        years.add(match[1])
    for match in re.finditer(r"\b(20\d{2})\s*[-:]?\s*kpss\b", text):
        years.add(match[1])
    return sorted(years)


def scoped_general_text(text, role):
    """Do not apply another cadre's certificate/postgraduate clause to this one."""
    role = fold(role)
    specific_roles = ("bilgisayar isletmeni", "buro personeli", "zabita", "itfaiye", "ogretim gorevlisi", "arastirma gorevlisi", "tekniker", "psikolog", "muhendis")
    blocks = re.split(r"\n(?=\s*(?:[a-z][.)]|\d+[.)])\s)", text)
    kept = []
    for block in blocks:
        normalized = re.sub(r"\s+", " ", fold(block))
        explicit_scope = bool(re.search(r"kadrosu|kadrosuna|kadrolari|kadrolarina|unvanina|unvanli", normalized))
        named = [name for name in specific_roles if name in normalized]
        if explicit_scope and named and not any(name in role for name in named):
            continue
        kept.append(block)
    return "\n".join(kept)


def assess(full_text, rows=None, general_text=None, title=""):
    rows = rows or []
    general_text = full_text if general_text is None else general_text
    if re.search(r"duzeltme\s+ilani|iptal\s+ilani|sonuc\s+duyurusu|sinav\s+sonucu|yerlestirme\s+sonucu", fold(title)):
        return {"is_suitable": False, "profile_verified": True, "reason": "Yeni personel alımı değil", "eligible_positions": []}
    eligible, rejected = [], []
    if not rows:
        types = score_types(full_text)
        scores = minimum_scores(full_text)
        has_unparsed_table = re.search(r"sira\s+no|kadro\s+(?:unvani|adedi|derecesi)", fold(full_text))
        if len(types) == 1 and scores and not has_unparsed_table:
            rows = [{"title": title, "qualification": full_text, "score_type": next(iter(types)), "minimum_score": max(scores), "quota": None}]
    for row in rows:
        row_general = scoped_general_text(general_text, row.get("title", ""))
        reason = exclusion(row_general) or exclusion(row.get("title", "") + " " + row.get("qualification", ""))
        if not reason and row.get("score_type") != PROFILE_TYPE:
            reason = "P3 dışında puan türü"
        if not reason and (row.get("minimum_score") is None or row["minimum_score"] > PROFILE_SCORE):
            reason = "Taban puan doğrulanamadı veya 78 üzerinde"
        if not reason and not accepts_degree(row.get("qualification", "")):
            reason = "Makine Mühendisliği / genel lisans şartı doğrulanamadı"
        local_years = required_kpss_years(row.get("qualification", "")) or required_kpss_years(row_general)
        if not reason and local_years and PROFILE_KPSS_YEAR not in local_years:
            reason = "Kadro 2026 KPSS puanını kabul etmiyor"
        if reason:
            rejected.append({"title": row.get("title", ""), "reason": reason})
        else:
            row = dict(row)
            row["required_kpss_years"] = local_years
            if fold(row.get("title", "")) == "muhendis" and re.search(r"makin[ae]\s+muhendis", fold(row.get("qualification", ""))):
                row["title"] = "Makine Mühendisi"
            eligible.append(row)
    # Optional profile fields reject only known mismatches; unknown ones are displayed for review.
    required_years = sorted({year for row in eligible for year in row.get("required_kpss_years", [])})
    scoped_blocks = list(dict.fromkeys(scoped_general_text(general_text, row["title"]) for row in eligible))
    conditions = extra_conditions("\n".join(scoped_blocks) + "\n" + "\n".join(row.get("qualification", "") for row in eligible))
    if eligible and not required_years:
        conditions.append({"label": "KPSS yılı", "text": "Kabul edilen sınav yılı kaynak metinde net okunamadı; resmî belgeyi kontrol et."})
    return {
        "is_suitable": bool(eligible), "profile_verified": bool(rows),
        "reason": "P3, taban puan ve diploma şartı eşleşti" if eligible else (rejected[0]["reason"] if rejected else "Kadro tablosu / puan / diploma şartı doğrulanamadı"),
        "eligible_positions": eligible, "rejected_position_count": len(rejected),
        "required_kpss_years": required_years,
        "extra_conditions": conditions,
    }
