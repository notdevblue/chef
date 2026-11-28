"""dashboard / render."""

import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlsplit

from chef.dashboard.assets import (
    DOCUMENT_VIEWER,
    DOCUMENT_VIEWER_SCRIPT,
    MARKDOWN_SCRIPT,
    REFRESH_SCRIPT,
    STYLE,
    THEME_CONTROL_SCRIPT,
    THEME_SCRIPT,
    THEME_TOGGLE,
)
from chef.dashboard.contracts import (
    APPROVED,
    CONFIRM_LETTERS,
    DOC_FIRST,
    HTML_TYPE,
    IDLE_SECONDS,
    IMAGE_TYPES,
    MAIN_LABEL,
    QUESTION_NAME,
    READ_ERRORS,
    REFRESH_SECONDS,
    RUNNING,
    SKIPPED,
    STAGE_TOKEN,
    SYSTEM_DOCUMENTS,
    TABLES,
)
from chef.dashboard.data import (
    collect,
    inspect_question,
    load_mission,
    mission_location,
    request_scoped,
)
from chef.dashboard.documents import document_visible, read_shared_text
from chef.dashboard.models import (
    ago,
    attempt,
    dated,
    esc,
    natural_key,
    raw_url,
    view_url,
)
from chef.guards import mission as mission_guard
from chef.mission import state as mission_state
from chef.questions import summary as question_summary
from chef.slots import commands as slots
from chef.slots import contracts as slots_contracts


def stale_tag(text):
    if not text:
        return ""
    short = text.split(",")[0].replace("끊김 의심: 마지막 활동 ", "끊김 ")
    return tag(short, "warn", f"{text} — {mission_guard.FORCE_CLOSE_HINT}")


def tag(text, kind="", title=""):
    return f'<span class="tag {kind}" title="{esc(title or text)}">{esc(text)}</span>'


def question_name(mission, name):
    summary = mission.questions.get(name)
    return (
        esc(question_label(name, summary)) + (" · 뼈대" if name in mission.skeletons else "")
        if summary
        else ""
    )


def question_prefix(name):
    if not QUESTION_NAME.fullmatch(name):
        return ""
    if name.startswith("ask-"):
        return f"질문 {name[4:-3]}"
    return f"확인 {name[8:-3]}"


def question_label(name, summary):
    prefix = question_prefix(name)
    title = summary.title.strip()
    normalized = re.sub(r"\s+", "", title).casefold()
    generic = {name.casefold(), prefix.casefold(), re.sub(r"\s+", "", prefix).casefold()}
    generic.add("질문" if name.startswith("ask-") else "확인")
    if name.startswith("confirm-"):
        generic.add(f"확인{name[8:-3]}".casefold())
    if normalized in generic:
        if summary.items:
            remainder = f" 외 {len(summary.items) - 1}개" if len(summary.items) > 1 else ""
            return f"{prefix} · {summary.items[0].title}{remainder}"
        return prefix or title
    if prefix and re.match(rf"^{re.escape(prefix)}(?:\s*[·:：-]\s*|\s+)", title, re.IGNORECASE):
        return title
    return f"{prefix} · {title}" if prefix else title


def markdown_heading(text):
    return next(
        (
            question_summary.plain_text(match.group(1))
            for line in question_summary.markdown_lines(text)
            if (match := re.match(r"^#{1}[ \t]+(.+?)[ \t]*#*[ \t]*$", line))
        ),
        "",
    )


def document_label(name, text, mission_title, folder=""):
    if QUESTION_NAME.fullmatch(name):
        if folder:
            summary, issue = inspect_question(folder, name, text)
            if not issue:
                return question_label(name, summary)
        return markdown_heading(text) or name
    title = mission_title if name == "mission.md" and mission_title else markdown_heading(text)
    prefix = "미션" if name == "mission.md" else "보고" if name == "report.md" else ""
    if not title or title.casefold() in (name.casefold(), prefix.casefold()):
        return prefix or name
    if prefix and re.match(rf"^{re.escape(prefix)}(?:\s*[·:：-]\s*|\s+)", title):
        return title
    return f"{prefix} · {title}" if prefix else title


def mission_url(mission):
    return f"/mission?slot={quote(mission.location_key or mission.slot, safe='')}&id={quote(mission.id, safe='')}"


def question_count_text(summary):
    if not summary.total_known:
        return "전체 항목 수 미확인"
    total = f"전체 {len(summary.items)}개"
    if not summary.count_known:
        return f"{total} · 미답 수 미확인"
    if summary.pending_count:
        return f"{total} · 미답 {summary.pending_count}개"
    return total


def question_status(summary):
    count_text = question_count_text(summary)
    if not summary.pending:
        return f"답 있음 · {count_text}"
    if not summary.total_known:
        return f"{count_text} · 상세 확인"
    if not summary.count_known or summary.pending_count:
        return count_text
    return f"{count_text} · 전체 승인 확인 대기"


def question_expand_label(summary):
    if not summary.count_known:
        return f"{question_status(summary)} 펼치기"
    if summary.pending_count:
        return f"미답 {summary.pending_count}개 펼치기"
    return "승인 확인 펼치기"


def question_items_html(items):
    return "".join(f"<li>{esc(f'{item.number} {item.title}'.strip())}</li>" for item in items)


def question_html(mission, name, compact=False, origin=""):
    summary = mission.questions[name]
    items = summary.items
    if summary.pending:
        items = [item for item in summary.items if item.status != "answered"]
    result = f'<div class="decision"><a class="decision-title" title="{question_name(mission, name)} · {esc(name)}" href="{view_url(os.path.join(mission.folder, name))}">{question_name(mission, name)} →</a>'
    if compact:
        result += f'<span class="document-name">{esc(question_status(summary))}</span>'
    else:
        result += f'<div class="counts">{esc(question_status(summary))}</div>'
    if compact and summary.pending:
        document_name = esc(name)
        if origin:
            document_name += f" · {esc(origin)}"
        result += f'<details data-key="question-{esc(mission.key)}-{esc(mission.id)}-{esc(name)}"><summary>{question_expand_label(summary)}</summary><p class="document-name">{document_name}</p>'
        if items:
            result += f'<ul class="question-items">{question_items_html(items)}</ul>'
        else:
            result += '<p class="muted">문항 목록 없음 · 상세 확인 필요</p>'
        result += "</details>"
    if not compact and items:
        result += f'<ul class="question-items">{question_items_html(items)}</ul>'
    return result + "</div>"


def question_issue_html(mission, name, origin=""):
    label = name + (f" · {origin}" if origin else "")
    path = os.path.join(mission.folder, name)
    title = (
        f'<a class="decision-title" href="{view_url(path)}">{esc(label)} →</a>'
        if name.endswith(".md") and os.path.isfile(path) and document_visible(mission.folder, path, set())
        else f'<span class="decision-title">{esc(label)}</span>'
    )
    return f'<div class="decision">{title}<p class="warn">{esc(mission.question_issues[name])}</p></div>'


def question_issue_badge(missions):
    count = sum(len(mission.question_issues) for mission in missions)
    return f"기록 확인 필요 {count}건" if count else ""


def known_pending_count(summaries):
    return sum(summary.pending_count for summary in summaries if summary.total_known)


def decision_badge(missions):
    summaries = [mission.questions[name] for mission in missions for name in mission.unanswered]
    if not summaries:
        return ""
    unknown = any(not summary.count_known for summary in summaries)
    count = known_pending_count(summaries)
    if unknown:
        if count:
            return f"답변 필요 {count}건 이상"
        return "답변 필요"
    if count:
        return f"답변 필요 {count}건"
    return "승인 확인 대기"


def waiting_text(mission):
    summaries = [mission.questions[name] for name in mission.unanswered]
    if not summaries:
        return "공유된 미답 질문 없음"
    count = known_pending_count(summaries)
    unknown = sum(not summary.count_known for summary in summaries)
    total_unknown = sum(not summary.total_known for summary in summaries)
    approval = sum(summary.count_known and not summary.pending_count for summary in summaries)
    parts = []
    if count:
        if unknown:
            parts.append(f"확인된 미답 항목 {count}개 이상")
        else:
            parts.append(f"미답 항목 {count}개")
    if total_unknown:
        parts.append(f"전체 항목 수 미확인 {total_unknown}문서")
    if unknown:
        parts.append(f"미답 수 미확인 {unknown}문서")
    if approval:
        parts.append(f"전체 승인 확인 대기 {approval}문서")
    return f"대기 문서 {len(summaries)}개 · " + " · ".join(parts)


def session_text(mission):
    if mission.state != RUNNING:
        return "당시 세션 기록"
    if not mission.active:
        return "세션 없음"
    if mission.background:
        return "하위 작업 중"
    if mission.waiting_input:
        return "입력 기다림"
    if not mission.last_activity:
        return "활동 기록 없음"
    return "작업 중" if time.time() - mission.last_activity <= IDLE_SECONDS else "조용함"


def freshness_html(mission):
    return (
        f'<div class="freshness"><span>업무 자료 갱신 {esc(dated(mission.state_updated))} · {esc(ago(mission.state_updated))}</span>'
        f"<span>세션: {esc(session_text(mission))} · 마지막 활동 {esc(dated(mission.last_activity))}</span></div>"
    )


def unit_status(value, include_token=False):
    meanings = {"todo": "다음 후보", "doing": "진행", "held": "보류", "done": "완료"}
    if include_token:
        return token_text(value, meanings)
    return meanings.get(value, value)


def recorded_value(value, empty="미기록"):
    if value.strip() in ("", "-"):
        return empty
    return value


def token_text(value, meanings, empty="미기록"):
    value = recorded_value(value, empty)
    if value in meanings:
        return f"{meanings[value]} ({value})"
    return value


def recorded_time_html(value, empty="시각 미기록"):
    if value.strip() in ("", "-"):
        return esc(empty)
    return f'<time datetime="{esc(value)}" title="{esc(value)}">{esc(value.replace("T", " ", 1))}</time>'


def record_text_html(value, empty="미기록", key=""):
    value = recorded_value(value, empty)
    text = f'<div class="record-text">{esc(value)}</div>'
    if len(value) > 600:
        return f'<details class="record-full" data-key="{esc(key)}"><summary>전문 펼치기 · {len(value):,}자</summary><div class="record-scroll">{text}</div></details>'
    return text


def record_fields_html(fields):
    return (
        '<dl class="record-fields">'
        + "".join(f"<dt>{esc(label)}</dt><dd>{value}</dd>" for label, value in fields)
        + "</dl>"
    )


def unit_dependencies(mission, row):
    conditions = []
    short_conditions = []
    dependencies = {unit["id"]: unit for unit in mission.units}
    for name in row["after"].split(","):
        name = name.strip()
        if name in ("", "-"):
            continue
        if name in dependencies:
            dependency = dependencies[name]
            conditions.append(f"선행 {name} {dependency['goal']} ({unit_status(dependency['status'])})")
            short_conditions.append(f"선행 {name} {unit_status(dependency['status'])}")
        else:
            conditions.append(f"선행 {name}: 등록된 작업을 찾을 수 없음 · 의존 상태 미확인")
            short_conditions.append(f"선행 {name} 미확인")
    return conditions, short_conditions


def unit_fields_html(mission, row, key):
    dependencies, _ = unit_dependencies(mission, row)
    after = record_text_html(row["after"], "선행 작업 미등록", key + "-after")
    after += "".join(f'<span class="condition">{esc(value)}</span>' for value in dependencies)
    timebox = recorded_value(row["timebox"], "시간 한도 미기록")
    if row["timebox"].strip() not in ("", "-"):
        timebox += "분"
    fields = [
        ("마일스톤 번호", esc(recorded_value(row["id"]))),
        ("작업 목표", record_text_html(row["goal"], "작업 목표 미기록", key + "-goal")),
        ("진행 상태", esc(unit_status(row["status"], include_token=True))),
        ("작업 범위 파일", record_text_html(row["files"], "작업 범위 파일 미등록", key + "-files")),
        ("완료 조건", record_text_html(row["done"], "완료 조건 미기록", key + "-done")),
        ("검증 계획", record_text_html(row["verify"], "검증 계획 미기록", key + "-verify")),
        ("시간 한도", esc(timebox)),
        ("금지·제약", record_text_html(row["forbid"], "금지·제약 미기록", key + "-forbid")),
        ("사람 확인 사항", record_text_html(row["human"], "사람 확인 사항 미기록", key + "-human")),
        ("선행 작업", after),
        ("변경 단위 (CL·브랜치)", record_text_html(row["unit"], "변경 단위 미등록", key + "-unit")),
        ("시작·재개 시각", recorded_time_html(row["started"])),
    ]
    return record_fields_html(fields)


def unit_heading_html(row, summarize_conditions):
    status = (
        f'<span class="muted">· <span class="record-token">{esc(unit_status(row["status"]))}</span></span>'
    )
    if summarize_conditions:
        return f'<div class="unit-summary"><b class="record-token">{esc(row["id"])}</b><span class="unit-goal">{esc(row["goal"])}</span>{status}</div>'
    return f"<b>{esc(row['id'])}</b> {esc(row['goal'])} {status}"


def unit_condition_summary(row, dependency_count):
    labels = []
    if dependency_count:
        labels.append(f"선행 작업 {dependency_count}개")
    if row["human"] not in ("", "-"):
        labels.append("사람 확인")
    if row["forbid"] not in ("", "-"):
        labels.append("금지·제약")
    return " · ".join(labels)


def unit_html(mission, row, compact=False, brief=False, key_prefix="work", summarize_conditions=False):
    conditions, short_conditions = unit_dependencies(mission, row)
    dependency_count = len(conditions)
    if row["human"] not in ("", "-"):
        conditions.append(f"사람 확인: {row['human']}")
        short_conditions.append("사람 확인")
    if row["forbid"] not in ("", "-"):
        conditions.append(f"금지: {row['forbid']}")
        short_conditions.append("금지 조건")
    content = unit_heading_html(row, summarize_conditions)
    if row["status"] == "doing" and mission_state.is_overdue(row):
        content += f' <span class="warn">시간 한도 {esc(mission_state.ratio_of(row))}</span>'
    if brief:
        return f"<li>{content}</li>"
    full_conditions = "".join(f'<span class="condition">{esc(condition)}</span>' for condition in conditions)
    if summarize_conditions:
        if conditions:
            content += f'<span class="condition">{esc(unit_condition_summary(row, dependency_count))}</span>'
    elif compact and conditions:
        key = f"unit-conditions-{key_prefix}-{mission.key}-{mission.id}-{row['id']}"
        content += f'<details data-key="{esc(key)}"><summary>{esc(" · ".join(short_conditions))}</summary>{full_conditions}</details>'
    else:
        content += full_conditions
    key = f"unit-{key_prefix}-{mission.key}-{mission.id}-{row['id']}"
    content += f'<details class="record unit-detail" data-key="{esc(key)}"><summary>작업 상세 펼치기</summary><div class="record-body">{unit_fields_html(mission, row, key)}</div></details>'
    return f"<li>{content}</li>"


def work_list(mission, rows, compact=False, brief=False, key_prefix="work", summarize_conditions=False):
    classes = "work-list"
    if brief:
        classes += " brief-work"
    return (
        f'<ul class="{classes}">'
        + "".join(unit_html(mission, row, compact, brief, key_prefix, summarize_conditions) for row in rows)
        + "</ul>"
    )


def current_html(mission, compact=False, summarize_conditions=False):
    if mission_state.UNITS_FILE in mission.errors:
        return '<p class="warn">작업 계획 읽기 실패 · 현재 작업 미확인</p>'
    if mission.doing:
        if compact:
            previews = " · ".join(f"{esc(row['id'])} {esc(row['goal'])}" for row in mission.doing[:2])
            if len(mission.doing) > 2:
                previews += f" · 진행 작업 {len(mission.doing)}개"
            key = f"current-{mission.key}-{mission.id}"
            return f'<details class="brief-details" data-key="{esc(key)}"><summary><span class="brief-clamp">{previews}</span></summary>{work_list(mission, mission.doing, key_prefix="current", summarize_conditions=summarize_conditions)}</details>'
        return work_list(
            mission, mission.doing, compact, key_prefix="current", summarize_conditions=summarize_conditions
        )
    text = "진행 중으로 기록된 작업 없음"
    if mission.unanswered:
        text += " · 공유된 질문 답변 대기"
    if mission.counts.get("held", 0):
        text += f" · 보류 작업 {mission.counts['held']}개 확인 필요"
    return f'<p class="muted">{text}</p>'


def remaining_html(mission, compact=False, summarize_conditions=False):
    if not mission.units:
        if mission_state.UNITS_FILE in mission.errors:
            return '<p class="muted">작업 계획 읽기 실패 · 남은 일 미확인</p>'
        return '<p class="muted">작업 계획 미등록</p>'
    rows = mission.remaining
    if not rows:
        return '<p class="muted">등록된 작업 모두 완료</p>'
    if compact:
        preview = next(
            (row for row in rows if row["status"] in ("todo", "held")),
            next((row for row in rows if row["status"] != "doing"), {}),
        )
        if preview:
            preview_html = work_list(mission, [preview], brief=True)
        else:
            preview_html = '<span class="muted">진행 중 작업만 남아 있음</span>'
        return (
            f'<div class="remaining-brief"><span class="counts">남은 {len(rows)}개</span>{preview_html}'
            + f'<details class="remaining-more" data-key="remaining-{esc(mission.key)}-{esc(mission.id)}"><summary>작업·조건 펼치기</summary>'
            + f'<div><p class="counts">남은 작업 {len(rows)}개 · 진행 중 포함</p>{work_list(mission, rows, key_prefix="remaining", summarize_conditions=summarize_conditions)}</div></details></div>'
        )
    return f'<p class="counts">남은 작업 {len(rows)}개 · 진행 중 포함</p>{work_list(mission, rows, key_prefix="remaining", summarize_conditions=summarize_conditions)}'


def label_text(label):
    return "메인" if label == MAIN_LABEL else label


def section_head(number, title):
    return f'<h2 class="sec"><span class="eyebrow">{number:02d} /</span>{esc(title)}</h2>'


def mission_fields_html(config, mission):
    meanings = {"A": "설계 확인", "B": "작업 계획 승인", "C": "결과·마지막 확인"}
    fields = []
    for name, value in mission.fields.items():
        label = name
        text = esc(recorded_value(value))
        if name == "현재 단계":
            token = (STAGE_TOKEN.match(value) or [""])[0]
            labels = {stage["id"]: stage.get("label") or stage["id"] for stage in config.stages}
            if token in labels:
                text = f"{esc(labels[token])} ({esc(value)})"
        elif name.startswith("확인 "):
            letter = name[3:4]
            if letter in meanings:
                label += " · " + meanings[letter]
        fields.append((label, text))
    return record_fields_html(fields)


def ledger_unit_text(mission, value):
    for unit in mission.units:
        if unit["id"] == value:
            return f"{value} · {unit['goal']}"
    return recorded_value(value, "대상 미기록")


def state_record_html(mission, file_name, row, number):
    key = f"record-{mission.key}-{mission.id}-{file_name}-{number}"
    if file_name == mission_state.LEDGER_FILE:
        status = token_text(
            row["status"], {"pass": "통과", "fail": "실패", "retry": "재시도", "blocked": "막힘"}
        )
        grade = token_text(
            row["grade"],
            {
                "editmode-pass": "Unity EditMode 검증",
                "pytest-pass": "pytest 검증",
                "compile-only": "컴파일만 확인",
                "doc-only": "문서 검증",
                "needs-human": "사람 확인 필요",
            },
        )
        title = ledger_unit_text(mission, row["unit"])
        meta = (
            f'<span class="record-token">판정: {esc(status)}</span><span>검증 수준: {esc(grade)}</span>'
            + recorded_time_html(row["at"])
        )
        fields = [
            ("기록 시각", recorded_time_html(row["at"])),
            ("검증 대상", esc(title)),
            ("판정 결과", esc(status)),
            ("검증 수준", esc(grade)),
            ("검증 근거", record_text_html(row["evidence"], "검증 근거 미기록", key + "-evidence")),
            (
                "기록된 관련·검증 대상 파일",
                record_text_html(row["files"], "관련·검증 대상 파일 미등록", key + "-files"),
            ),
        ]
    elif file_name == mission_state.DECISIONS_FILE:
        title = f"{recorded_value(row['id'], '결정 번호 미기록')} · {recorded_value(row['what'], '논의 내용 미기록')}"
        meta = recorded_time_html(row["at"])
        fields = [
            ("기록 시각", recorded_time_html(row["at"])),
            ("결정 번호", esc(recorded_value(row["id"], "결정 번호 미기록"))),
            ("논의 내용", record_text_html(row["what"], "논의 내용 미기록", key + "-what")),
            ("선택 결과", record_text_html(row["chosen"], "선택 결과 미기록", key + "-chosen")),
            ("이유", record_text_html(row["why"], "이유 미기록", key + "-why")),
            ("영향 범위", record_text_html(row["affects"], "영향 범위 미기록", key + "-affects")),
        ]
    else:
        title = token_text(
            row["action"], {"open": "미션 시작 연결", "resume": "미션 이어서 연결"}, "이벤트 미기록"
        )
        meta = recorded_time_html(row["at"])
        fields = [
            ("연결 기록 시각", recorded_time_html(row["at"])),
            ("연결 이벤트", esc(title)),
            ("대화 ID", record_text_html(row["session"], "대화 ID 미기록", key + "-session")),
            ("로그 경로", record_text_html(row["transcript"], "로그 경로 미기록", key + "-transcript")),
        ]
    return (
        f'<li><details class="record" data-key="{esc(key)}"><summary><span class="record-title">{esc(title)}</span>'
        f'<span class="record-meta">{meta}</span><span class="muted">기록 상세 펼치기</span></summary>'
        f'<div class="record-body">{record_fields_html(fields)}</div></details></li>'
    )


def state_records_html(mission, file_name, rows):
    if file_name == mission_state.UNITS_FILE:
        return work_list(mission, rows, key_prefix="source", summarize_conditions=True)
    return (
        '<ul class="record-list">'
        + "".join(
            state_record_html(mission, file_name, row, number)
            for number, row in reversed(list(enumerate(rows, start=1)))
        )
        + "</ul>"
    )


def mission_row(mission, index):
    historical = mission.state != RUNNING
    badges = tag(mission.state, "dim" if historical else "")
    if historical:
        badges += tag("당시 기록", "dim")
    elif mission.active:
        badges += tag("ACTIVE", "on") + stale_tag(mission.stale)
    elif mission.state == RUNNING:
        badges += tag("세션 없음", "warn")
    if mission.errors:
        badges += tag(f"읽기 실패: {', '.join(mission.errors)}", "warn")
    if mission.question_issues:
        badges += tag(question_issue_badge([mission]), "warn")
    body = f'<a class="name" href="{mission_url(mission)}">{esc(mission.title or mission.id)}</a> {badges}'
    plan_state = "작업 계획 읽기 실패" if mission_state.UNITS_FILE in mission.errors else "작업 계획 미등록"
    body += (
        f'<div class="stage">{esc(mission.stage)} · 완료 {mission.counts["done"]}/{len(mission.units)} · 남음 {len(mission.remaining)}</div>'
        if mission.units
        else f'<div class="stage">{plan_state}</div>'
    )
    meta = f"업무 자료 {esc(dated(mission.state_updated))}<br>세션 {esc(session_text(mission))}"
    return f'<div class="row"><span class="no">{index:02d}</span><div>{body}</div><div class="meta">{meta}</div><a class="arrow" href="{mission_url(mission)}">→</a></div>'


def slot_section(location):
    row = location.row
    meta = ""
    if location.label != MAIN_LABEL:
        state = row.get("state", "-")
        meta += tag(f"synced {row.get('synced', '-')}", "dim") + tag(
            f"state {state}", "" if state == "ready" else "warn"
        )
    if location.active.corrupt:
        meta += tag("ACTIVE 손상", "warn")
    elif location.active.id:
        meta += tag(f"ACTIVE {slots.active_text(location.active)}", "on") + stale_tag(location.stale)
    if location.problem:
        meta += tag(location.problem, "warn")
    running = [mission for mission in location.missions if mission.state == RUNNING]
    folded = [mission for mission in location.missions if mission.state != RUNNING]
    rows = "".join(mission_row(mission, number) for number, mission in enumerate(running, start=1))
    out = f'<h3 class="sub" title="{esc(location.root)}">{esc(label_text(location.label))}</h3><div class="workspace-path">{esc(location.root)}</div><div>{meta}</div>'
    out += f'<div class="list">{rows}</div>' if rows else '<p class="muted">진행 중 미션 없음</p>'
    if folded:
        inner = "".join(mission_row(mission, number) for number, mission in enumerate(folded, start=1))
        out += f'<details data-key="{esc(location.key or location.label)}"><summary>중단·닫힘 {len(folded)}개</summary><div class="list">{inner}</div></details>'
    return out


def confirm_box(mission, letter):
    main = mission.confirms.get(letter, "")
    parts = {key: value for key, value in mission.confirms.items() if key[0] == letter and key != letter}
    if main.startswith(APPROVED):
        kind = "full"
    elif main.startswith(SKIPPED):
        kind = "skip"
    elif APPROVED in main or any(value.startswith(APPROVED) for value in parts.values()):
        kind = "half"
    else:
        kind = ""
    title = (
        " / ".join(f"{key} {value}" for key, value in mission.confirms.items() if key[0] == letter)
        or f"{letter} 없음"
    )
    return f'<span title="{esc(title)}">{letter}<span class="box {kind}"></span></span>'


def stage_track(config, mission):
    token = (STAGE_TOKEN.match(mission.stage) or [""])[0]
    ids = [stage["id"] for stage in config.stages]
    current = ids.index(token) if token in ids else -1
    steps = "<i>›</i>".join(
        f'<span class="{"cur" if index == current else "later" if index > current else ""}">{esc(stage.get("label") or stage["id"])}</span>'
        for index, stage in enumerate(config.stages)
    )
    if current < 0:
        steps += f'<span class="cur" title="{esc(mission.stage)}">{esc(token or mission.stage)}</span>'
    return f'<div class="track" title="{esc(mission.stage)}">{steps}</div>'


def slot_card(config, location):
    running = [mission for mission in location.missions if mission.state == RUNNING]
    head = f'<div class="card-top"><span class="slot" title="{esc(location.root)}">{esc(label_text(location.label))}</span>'
    head += f'<span class="workspace-path" title="{esc(location.root)}">{esc(location.root)}</span>'
    if not running:
        state = "ACTIVE 손상" if location.active.corrupt else "비어 있음"
        return f'<div class="card idle">{head}<span class="state">{state}</span></div><span class="counts">진행 중 미션 없음</span></div>'
    mission = next((item for item in running if item.active), running[0])
    kind = "turn" if any(item.unanswered or item.question_issues for item in running) else ""
    head += f'<a class="mission-name" title="{esc(mission.title or mission.id)}" href="{mission_url(mission)}">미션 상세 →</a>'
    state = tag(mission.state) + (
        tag("ACTIVE") + stale_tag(mission.stale) if mission.active else tag("세션 없음", "warn")
    )
    if location.active.corrupt:
        state += tag("ACTIVE 손상", "warn")
    if mission.errors:
        state += tag(f"읽기 실패: {', '.join(mission.errors)}", "warn")
    badge = decision_badge(running)
    if badge:
        state += tag(badge, "warn")
    issue_badge = question_issue_badge(running)
    if issue_badge:
        state += tag(issue_badge, "warn")
    plan_state = "작업 계획 읽기 실패" if mission_state.UNITS_FILE in mission.errors else "작업 계획 미등록"
    progress = (
        f"마일스톤 완료 {mission.counts['done']}/{len(mission.units)} · 남음 {len(mission.remaining)}"
        if mission.units
        else plan_state
    )
    goal_preview = next((line.strip() for line in mission.goal.splitlines() if line.strip()), "")
    goal = (
        f'<details class="brief-details goal-details" data-key="goal-{esc(mission.key)}-{esc(mission.id)}"><summary class="goal"><span class="goal-preview">{esc(goal_preview)}</span><span class="goal-collapse">목표 접기</span></summary><p class="goal">{esc(mission.goal)}</p></details>'
        if mission.goal
        else '<p class="muted">목표 기록 없음</p>'
    )
    questions = "".join(
        question_html(item, name, compact=True, origin=item.id if item is not mission else "")
        for item in running
        for name in item.unanswered
    )
    issues = "".join(
        question_issue_html(item, name, item.id if item is not mission else "")
        for item in running
        for name in item.question_issues
    )
    issue_section = "<section><h3>기록 확인 필요</h3><div>" + issues + "</div></section>" if issues else ""
    others = "".join(
        f'<a href="{mission_url(item)}">{esc(item.title or item.id)} →</a>'
        for item in running
        if item is not mission
    )
    return (
        f'<article class="card {kind}" data-slot="{esc(location.key or location.label)}">{head}<span class="state">{state}</span></div>'
        + stage_track(location, mission)
        + '<div class="mission-grid"><section><h3>목표</h3><div>'
        + goal
        + "</div></section><section><h3>내 결정</h3><div>"
        + (questions or '<span class="muted">공유된 미답 질문 없음</span>')
        + "</div></section>"
        + issue_section
        + "<section><h3>지금</h3><div>"
        + current_html(mission, compact=True)
        + "</div></section><section><h3>다음</h3><div>"
        + remaining_html(mission, compact=True)
        + "</div></section></div>"
        + f'<div class="summary-meta"><span>{progress}</span><span>자료 갱신 {esc(ago(mission.state_updated))}</span><span>세션: {esc(session_text(mission))}</span></div>'
        + '<details class="card-meta"><summary>승인·활동 상세</summary>'
        + '<div class="boxes">'
        + "".join(confirm_box(mission, letter) for letter in CONFIRM_LETTERS)
        + "</div>"
        + freshness_html(mission)
        + "</details>"
        + (
            f'<div class="other-missions">같은 슬롯의 다른 진행 미션 {len(running) - 1}개: {others}</div>'
            if others
            else ""
        )
        + "</article>"
    )


def page(title, body, right="", script="", preview=False, embedded=False):
    mast, foot, theme = "", "", ""
    if not embedded:
        theme = THEME_SCRIPT
        mast = f'<header class="mast"><div class="in"><h1>{esc(title)}</h1><div class="mast-r">{right}{THEME_TOGGLE}</div></div></header>{THEME_CONTROL_SCRIPT}'
        foot = '<footer class="foot"><div class="in"><span>chef missions</span><span>local · 127.0.0.1</span></div></footer>'
    if preview:
        body += DOCUMENT_VIEWER
        script += DOCUMENT_VIEWER_SCRIPT
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{theme}<style>{STYLE}</style></head>
<body>{mast}
<main class="wrap">{body}</main>
{foot}
{script}</body></html>"""


def message(status, text):
    return (
        status,
        HTML_TYPE,
        page(str(status), f'<p>{esc(text)}</p><p><a href="/">← 개요</a></p>').encode("utf-8"),
    )


@request_scoped
def overview_page(config):
    found = collect(config, overview=True)
    waiting = [
        (mission, name)
        for location in found
        for mission in location.missions
        if mission.state == RUNNING
        for name in mission.unanswered
    ]
    visible = [location for location in found if location.label != MAIN_LABEL or location.missions]
    occupied = [
        location for location in visible if any(mission.state == RUNNING for mission in location.missions)
    ]
    occupied.sort(
        key=lambda location: (
            not any(
                mission.state == RUNNING and (mission.unanswered or mission.question_issues)
                for mission in location.missions
            )
        )
    )
    idle = [location for location in visible if location not in occupied]
    cards = [slot_card(config, location) for location in occupied]
    body = section_head(1, "미션 작업 공간")
    if getattr(config, "observatory_report", None):
        body += '<p><a href="/run-metrics">실행 통계 →</a></p>'
    body += "".join(f'<p class="warn">{esc(location.problem)}</p>' for location in found if location.problem)
    body += f'<div class="board">{"".join(cards)}</div>'
    if idle:
        body += (
            '<div class="idle-board">' + "".join(slot_card(config, location) for location in idle) + "</div>"
        )
    body += section_head(2, "답 기다리는 질문")
    if waiting:
        rows = "".join(
            f'<a class="row link" href="{view_url(os.path.join(mission.folder, name))}"><span class="no">{number:02d}</span>'
            f'<span>{question_name(mission, name)}<br><span class="document-name">{esc(name)}</span><span class="counts">{question_status(mission.questions[name])}</span></span><span class="meta">{esc(label_text(mission.slot))} · {esc(mission.id)}</span>'
            f'<span class="arrow">→</span></a>'
            for number, (mission, name) in enumerate(waiting, start=1)
        )
        body += f'<div class="invert"><div class="list">{rows}</div></div>'
    else:
        body += '<p class="muted">공유된 미답 질문 없음</p>'
    issues = [
        (mission, name)
        for location in found
        for mission in location.missions
        if mission.state == RUNNING
        for name in mission.question_issues
    ]
    if issues:
        body += section_head(3, "기록 확인 필요") + f'<p class="counts">{len(issues)}건</p>'
        body += (
            '<div class="list">'
            + "".join(
                f'<div class="row"><span class="no">{number:02d}</span><div>{question_issue_html(mission, name)}</div><a class="meta" href="{mission_url(mission)}">{esc(label_text(mission.slot))} · {esc(mission.id)} →</a></div>'
                for number, (mission, name) in enumerate(issues, start=1)
            )
            + "</div>"
        )
    body += (
        '<details data-key="mission-index"><summary>전체 미션·중단·닫힘 기록</summary>'
        + "".join(slot_section(location) for location in found)
        + "</details>"
    )
    right = (
        f"<span>갱신 {datetime.now().strftime('%H:%M:%S')}</span>"
        f'<label class="refresh-toggle"><input type="checkbox" id="auto"> {REFRESH_SECONDS}초 자동 새로고침</label>'
    )
    script = REFRESH_SCRIPT.replace("__REFRESH_MS__", str(REFRESH_SECONDS * 1000))
    return 200, HTML_TYPE, page("Missions", body, right, script, preview=True).encode("utf-8")


def poc_entry(root, poc):
    if os.path.isfile(os.path.join(root, poc)):
        return raw_url(os.path.join(root, poc)), esc(f"PoC · {poc}")
    return "", tag(f"PoC 파일 없음: {poc}", "warn")


def file_links(folder, names, lead=(), mission_title=""):
    entries = [(href, label, "") for href, label in lead]
    for name in names:
        path = os.path.join(folder, name)
        try:
            text = read_shared_text(folder, path) if name.endswith(".md") and name != "mission.md" else ""
        except READ_ERRORS:
            text = ""
        label = document_label(name, text, mission_title, folder) if name.endswith(".md") else name
        entries.append((view_url(path), esc(label), name))
    rows = "".join(
        (f'<a class="row link" href="{href}">' if href else '<div class="row">')
        + f'<span class="no">{number:02d}</span><span>{label}'
        + (f'<br><span class="document-name">{esc(name)}</span>' if name else "")
        + '</span><span class="meta"></span>'
        + ('<span class="arrow">→</span></a>' if href else "<span></span></div>")
        for number, (href, label, name) in enumerate(entries, start=1)
    )
    return f'<div class="list">{rows}</div>' if rows else '<p class="muted">없음</p>'


@request_scoped
def mission_page(config, slot, mission_id):
    if not (
        slot == MAIN_LABEL or slots_contracts.NAME_PATTERN.fullmatch(slot)
    ) or not mission_guard.valid_mission_id(mission_id):
        return message(400, "slot·id 형식 오류")
    location = mission_location(config, slot, mission_id)
    if not location:
        return message(404, f"미션 없음: {slot}/{mission_id}")
    mission = load_mission(location, mission_id)
    folder = mission.folder
    shared = attempt(
        mission.errors, mission_state.SHARED_DOCUMENTS_FILE, folder, mission_state.shared_documents, set()
    )
    automatic = attempt(mission.errors, "질문 문서", folder, mission_state.auto_question_documents, set())
    names = sorted(
        name
        for name in shared | automatic | SYSTEM_DOCUMENTS
        if os.path.isfile(os.path.join(folder, name))
        and document_visible(folder, os.path.join(folder, name), shared)
    )
    captures = sorted(
        (
            name
            for name in names
            if name.startswith("captures/") and os.path.splitext(name)[1].lower() in IMAGE_TYPES
        ),
        key=natural_key,
    )
    badges = tag(label_text(location.label), "dim") + tag(
        mission.state, "" if mission.state == RUNNING else "dim"
    )
    if mission.active:
        badges += tag("ACTIVE", "on") + stale_tag(mission.stale)
    elif mission.state == RUNNING:
        badges += tag("세션 없음", "warn")
    else:
        badges += tag("중단·닫힘 당시 기록", "dim")
    if mission.errors:
        badges += tag(f"읽기 실패: {', '.join(mission.errors)}", "warn")
    body = f"<p>{badges}</p>" + freshness_html(mission)
    body += (
        '<section class="work-section"><h2>목표</h2>'
        + (
            f'<p class="goal">{esc(mission.goal)}</p>'
            if mission.goal
            else '<p class="muted">목표 기록 없음</p>'
        )
        + "</section>"
    )
    body += (
        '<section class="work-section" id="current"><h2>지금 하는 일</h2>'
        + current_html(mission, summarize_conditions=True)
        + "</section>"
    )
    body += (
        '<section class="work-section" id="remaining"><h2>다음·남은 일</h2>'
        + remaining_html(mission, summarize_conditions=True)
        + "</section>"
    )
    body += (
        '<section class="work-section"><h2>내 결정</h2>'
        + f'<p class="counts">{esc(waiting_text(mission))}</p>'
    )
    body += "".join(question_html(mission, name) for name in mission.unanswered) + "</section>"
    if mission.question_issues:
        body += (
            '<section class="work-section" id="question-issues"><h2>기록 확인 필요</h2>'
            + f'<p class="counts">{len(mission.question_issues)}건</p>'
        )
        body += "".join(question_issue_html(mission, name) for name in mission.question_issues) + "</section>"
    completed = [row for row in mission.units if row["status"] == "done"]
    if mission_state.UNITS_FILE in mission.errors:
        body += '<details data-key="completed"><summary>완료 작업 미확인</summary><p class="warn">작업 계획 읽기 실패 · 완료 작업 미확인</p></details>'
    else:
        body += f'<details data-key="completed"><summary>완료 작업 {len(completed)}개</summary>{work_list(mission, completed, key_prefix="completed", summarize_conditions=True)}</details>'
    if mission.now and "report.md" in shared:
        body += f'<details data-key="recent-report"><summary>최근 보고 · {esc(dated(mission.report_updated))}</summary><p class="counts">보고 당시 요약</p><div class="now">{esc(mission.now)}</div></details>'
    questions = sorted((name for name in mission.questions if name in names), key=natural_key)
    archived = [name for name in mission.archived_questions if name in names]
    excluded = set(questions) | set(mission.question_issues) | set(archived)
    docs = sorted(
        (name for name in names if name.endswith(".md") and name not in excluded),
        key=lambda name: (name not in DOC_FIRST, name),
    )
    others = [name for name in names if name not in excluded and name not in docs and name not in captures]
    body += '<details data-key="source-documents"><summary>질문·확인 원문·문서·파일</summary>'
    body += section_head(1, "질문·확인 원문")
    marked = "".join(
        f'<div class="row"><span class="no">{number:02d}</span><div>{question_html(mission, name)}</div>'
        f'<span class="meta">{esc(question_status(mission.questions[name]))}</span><a class="arrow" href="{view_url(os.path.join(folder, name))}">→</a></div>'
        for number, name in enumerate(questions, start=1)
    )
    body += f'<div class="list">{marked}</div>' if marked else '<p class="muted">공유된 질문 없음</p>'
    poc = mission.fields.get("PoC", "")
    body += section_head(2, "문서") + file_links(
        folder, docs, [poc_entry(location.root, poc)] if poc else [], mission.title
    )
    if others:
        body += section_head(3, "기타 파일") + file_links(folder, others)
    if captures:
        thumbs = "".join(
            f'<a href="{view_url(os.path.join(folder, name))}"><img loading="lazy" src="{view_url(os.path.join(folder, name))}" alt="{esc(name)}"></a>'
            for name in captures
        )
        body += section_head(4, "캡처") + f'<div class="thumbs">{thumbs}</div>'
    if archived:
        body += "<h3>이전 형식의 질문 원문</h3>" + file_links(folder, archived, mission_title=mission.title)
    body += (
        '</details><details data-key="source-state"><summary>미션 상태·마일스톤·검증·결정·연결 기록</summary>'
    )
    body += mission_fields_html(location, mission)
    descriptions = {
        mission_state.UNITS_FILE: "작업 목표와 진행 상태 · 완료 조건과 검증 계획은 실행 결과가 아닙니다.",
        mission_state.LEDGER_FILE: "최근 기록부터 표시 · 판정 결과와 검증 수준은 별개이며 통과는 제출·승인을 뜻하지 않습니다.",
        mission_state.DECISIONS_FILE: "최근 기록부터 표시 · 당시 논의와 선택 결과입니다.",
        mission_guard.SESSIONS_FILE: "최근 연결 기록부터 표시 · 현재 실행 여부는 상단 세션 상태에서 확인합니다.",
    }
    for number, (title, file_name, header) in enumerate(TABLES, start=1):
        body += f'<section data-table="{esc(file_name)}">' + section_head(number, title)
        body += f'<p class="counts">{esc(descriptions[file_name])}</p>'
        if file_name in names:
            body += f'<p class="record-source"><a href="{view_url(os.path.join(folder, file_name))}">원본 {esc(file_name)} 보기 →</a></p>'
        errors = []
        rows = attempt(
            errors,
            file_name,
            os.path.join(folder, file_name),
            lambda p, h=header: mission_state.read_table(p, h),
            [],
        )
        if errors:
            body += tag(f"읽기 실패: {file_name} · 기록 미확인", "warn")
        elif not os.path.isfile(os.path.join(folder, file_name)):
            body += '<p class="muted">파일 미등록 · 기록 미확인</p>'
        elif rows:
            body += f'<p class="counts">기록 {len(rows)}건</p>' + state_records_html(mission, file_name, rows)
        else:
            body += '<p class="muted">기록 0건 · 아직 기록된 항목 없음</p>'
        body += "</section>"
    body += "</details>"
    return (
        200,
        HTML_TYPE,
        page(mission.title or mission_id, body, '<a href="/">← 개요</a>', preview=True).encode("utf-8"),
    )
