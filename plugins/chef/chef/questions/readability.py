"""Advisory prose diagnostics. Never rewrite content or decide approval validity."""

import re
from collections import defaultdict


SENTENCE_LIMIT = 160
ITEM_LIMIT = 320
LIST_LIMIT = 12
SECTION_LIMIT = 1500
DUPLICATE_MIN = 40


def table_cells(text):
    boundaries = [match.end() - 1 for match in re.finditer(r'(\\*)\|', text) if len(match.group(1)) % 2 == 0]
    cells = []
    start = 0
    for boundary in boundaries:
        cells.append(text[start:boundary])
        start = boundary + 1
    cells.append(text[start:])
    if boundaries and boundaries[0] == 0:
        cells = cells[1:]
    if boundaries and boundaries[-1] == len(text) - 1:
        cells = cells[:-1]
    return cells


def visible(text):
    """Mask technical spans without moving sentence boundaries in the source."""
    chars = list(text)
    def escaped(index):
        count = 0
        while index > 0 and text[index - 1] == "\\":
            count += 1
            index -= 1
        return count % 2 == 1

    for match in re.finditer(r"(`+)", text):
        start = match.start()
        if chars[start] == " " or escaped(start):
            continue
        marker = match.group()
        for closing in re.finditer(r"`+", text[match.end():]):
            if closing.group() == marker:
                end = match.end() + closing.end()
                chars[start:end] = " " * (end - start)
                break
    masked = "".join(chars)
    masked = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) + " " * (len(m.group()) - len(m.group(1))), masked)
    def mask_url(match):
        value = match.group()
        body = value.rstrip('.,!?;:)]}')
        return ' ' * len(body) + value[len(body):]

    masked = re.sub(r"https?://[^\s<>]+", mask_url, masked)
    masked = re.sub(r"<[^>]*>", lambda m: " " * len(m.group()), masked)
    return masked


def prose_units(text, location):
    """Read prose paragraphs and list entries; exclude tables and code blocks."""
    units = []
    lines = []
    bullet = False
    start = 1
    section = location
    fence = ""
    table = False
    source_lines = text.splitlines()

    def flush():
        nonlocal lines
        if lines:
            units.append((section, f"{location}:L{start}", " ".join(lines), bullet))
        lines = []

    for number, line in enumerate(source_lines, 1):
        stripped = line.strip()
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            flush()
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)", stripped)
        if heading:
            flush()
            table = False
            section = f"{location}/{heading.group(1)}"
            continue
        separator = r'\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*'
        next_separator = number < len(source_lines) and re.fullmatch(separator, source_lines[number])
        if stripped.startswith('|') or ('|' in line and (table or next_separator)):
            flush()
            table = True
            if not re.fullmatch(r'[\s|:\-]+', stripped):
                cells = table_cells(stripped)
                for index, cell in enumerate(cells):
                    if re.search(r'\s', cell.strip()):
                        units.append((section, f'{location}:L{number}:C{index + 1}', cell.strip(), None))
            continue
        table = False
        if not stripped or stripped.startswith("<a ") or line.startswith("    "):
            flush()
            continue
        match = re.match(r"^\s*(?:[-*+] |\d+[.)] )", line)
        if match:
            flush()
            bullet = True
            start = number
            lines = [line[match.end():]]
        else:
            if not lines:
                start = number
                bullet = False
            lines.append(stripped)
    flush()
    return units


def record_units(record):
    units = []

    def add(text, location):
        units.extend(prose_units(text, location))

    add(record.get("intro", ""), "intro")
    approval = record.get("approval", {})
    if not any(a.get("approval") == approval for a in record.get("answers", [])):
        add(approval.get("reason", ""), "approval.reason")
    for item in record.get("items", []):
        pending = item.get("status") == "pending"
        if pending:
            add(item.get("body", ""), f"items.{item['id']}.body")
        if pending or (item.get("selected") and record.get("presentation") != "decision"):
            for option in item.get("options", []):
                if pending or option['id'] == item.get("selected"):
                    text = "\n".join(option.get(key, "") for key in ("description", "impact"))
                    units.append(("questions", f"items.{item['id']}.options.{option['id']}", text, True))
    if record.get("body"):
        add(record["body"], "body")
    for name in ("blocks", "detail_blocks"):
        for block in record.get(name, []):
            path = f"{name}.{block['id']}"
            kind = block['type']
            if kind == "paragraph":
                add(block.get("text", " ".join(block.get("sentences", []))), path)
            elif kind in ("list", "rules", "steps"):
                for entry in block['items']:
                    values = [entry.get(key, "") for key in ("text", "result", "when", "failure")]
                    values.extend(entry.get("conditions", {}).get("items", []))
                    units.append((path, f"{path}.{entry['id']}", "\n".join(values), True))
            elif kind in ("fields", "schema-table"):
                for field in block['fields']:
                    units.append((path, f"{path}.{field['name']}", field.get('description', ''), None))
            elif kind == "table":
                for ri, row in enumerate(block['rows']):
                    for ci, cell in enumerate(row):
                        if re.search(r"\s", cell):
                            units.append((path, f"{path}.rows[{ri}][{ci}]", cell, None))
    return units


def analyze_units(units, document='document'):
    notes = []
    sections = defaultdict(lambda: [0, 0])
    repetitions = defaultdict(set)
    total = [0, 0]

    def note(code, location, message, **metrics):
        notes.append({"code": code, "location": location, "message": message, **metrics})

    for section, location, text, bullet in units:
        masked = visible(text)
        count = len(re.sub(r"\s+", " ", masked).strip())
        if bullet is not None:
            sections[section][0] += int(bullet)
            sections[section][1] += count
            total[0] += int(bullet)
            total[1] += count
        if count > ITEM_LIMIT:
            note("long_item", location, "중복·근거 설명을 줄이고 독립된 규칙인지 확인하세요.", characters=count, limit=ITEM_LIMIT)
        start = 0
        for boundary in [*re.finditer(r"\n+|(?<=[.!?。！？])\s+", masked), None]:
            end = boundary.start() if boundary else len(masked)
            sentence = re.sub(r"\s+", " ", masked[start:end]).strip()
            if len(sentence) > SENTENCE_LIMIT:
                note("long_sentence", location, "문장을 단순화하세요. 길이만 맞추려고 불릿을 늘리지 마세요.", characters=len(sentence), limit=SENTENCE_LIMIT)
            original = re.sub(r"\s+", " ", text[start:end]).strip()
            if len(original) >= DUPLICATE_MIN and sentence:
                repetitions[original].add(location)
            start = boundary.end() if boundary else end
    for section, (bullets, count) in sections.items():
        if bullets > LIST_LIMIT and count > SECTION_LIMIT:
            note("dense_list", section, "설명 문단·비교 표·현재 결정으로 묶을 수 있는지 확인하세요.", bullets=bullets, characters=count)
    if total[0] > 40 and total[1] > 4000:
        note("dense_document", document, "절을 나눠도 전체 설명 부담이 남습니다. 중복과 현재 판단 범위를 확인하세요.", bullets=total[0], characters=total[1])
    for sentence, locations in repetitions.items():
        if len(locations) > 1:
            places = sorted(locations)
            note("duplicate_sentence", places[0], "동일 설명이 여러 위치에 있습니다. 한 곳의 계약으로 정리할지 확인하세요.", locations=places, text=sentence)
    return notes


def analyze(record):
    units = record_units(record)
    main = [unit for unit in units if not unit[1].startswith('detail_blocks.')]
    detail = [unit for unit in units if unit[1].startswith('detail_blocks.')]
    return analyze_units(main) + analyze_units(detail, 'detail_document')


def analyze_markdown(text):
    return analyze_units(prose_units(text, "document"))


def messages(record):
    result = []
    for note in analyze(record):
        counts = []
        if 'characters' in note:
            counts.append(f"{note['characters']}자")
        if 'limit' in note:
            counts.append(f"기준 {note['limit']}자")
        if 'bullets' in note:
            counts.append(f"항목 {note['bullets']}개")
        if 'locations' in note:
            counts.append(' · '.join(note['locations']))
        measurement = ' (' + ', '.join(counts) + ')' if counts else ''
        result.append(f"문서 구성 확인 [{note['code']}] {note['location']}{measurement}: {note['message']}")
    return result
