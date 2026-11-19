"""dashboard / server."""

import argparse
import copy
import functools
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from chef.dashboard import discovery as dashboard_discovery
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
    ACTIVITY_TYPES,
    APPROVED,
    ASSET_TYPES,
    BINARY_PROBE_BYTES,
    CODEX_ROLLOUT,
    CODEX_TURN_DONE,
    CODEX_TURN_EVENTS,
    CONFIRM_LETTERS,
    DEFAULT_PORT,
    DOC_FIRST,
    FILE_URL,
    HANDBACK_TOOL,
    HTML_TYPE,
    IDLE_SECONDS,
    IMAGE_TYPES,
    INSPECTION_SETTLE_NS,
    JS_TYPE,
    MAIN_LABEL,
    MARKED_URL,
    MAX_TEXT_BYTES,
    NATURAL_SPLIT,
    NO_INSPECTION,
    QUESTION_NAME,
    QUESTION_RECORD_HEADER,
    RAW_PREFIX,
    READ_ERRORS,
    REFRESH_SECONDS,
    RUNNING,
    SKELETON_HEADING,
    SKILLS_DIR,
    SKIPPED,
    STAGE_TOKEN,
    SUBAGENT_FILE,
    SUBAGENT_STALE_SECONDS,
    SYSTEM_DOCUMENTS,
    TABLES,
    TASK_NOTIFICATION,
    TEXT_TYPE,
    TRANSCRIPT_TAIL_BYTES,
    UTF8_SUFFIXES,
)
from chef.dashboard.data import (
    INSPECTION_LIMIT,
    INSPECTIONS,
    INSPECTIONS_LOCK,
    codex_parent_of,
    collect,
    inspect_question,
    load_mission,
    locations,
    mission_folder_allowed,
    read_session,
    read_turn,
    recent_subagents,
    request_scoped,
    stale_of,
    transcript_of,
)
from chef.dashboard.documents import (
    digest,
    document_visible,
    read_shared_text,
    shared_bytes_match,
)
from chef.dashboard.models import (
    Location,
    Mission,
    ago,
    attempt,
    dated,
    esc,
    fresh,
    modified_at,
    natural_key,
    parse_time,
    raw_url,
    section_text,
    view_url,
)
from chef.dashboard.render import (
    confirm_box,
    current_html,
    decision_badge,
    document_label,
    file_links,
    freshness_html,
    known_pending_count,
    label_text,
    ledger_unit_text,
    markdown_heading,
    message,
    mission_fields_html,
    mission_page,
    mission_row,
    mission_url,
    overview_page,
    page,
    poc_entry,
    question_count_text,
    question_expand_label,
    question_html,
    question_issue_badge,
    question_issue_html,
    question_items_html,
    question_label,
    question_name,
    question_prefix,
    question_status,
    record_fields_html,
    record_text_html,
    recorded_time_html,
    recorded_value,
    remaining_html,
    section_head,
    session_text,
    slot_card,
    slot_section,
    stage_track,
    stale_tag,
    state_record_html,
    state_records_html,
    tag,
    token_text,
    unit_condition_summary,
    unit_dependencies,
    unit_fields_html,
    unit_heading_html,
    unit_html,
    unit_status,
    waiting_text,
    work_list,
)
from chef.guards import mission as mission_guard
from chef.infrastructure import configuration as infrastructure_configuration
from chef.infrastructure import errors as infrastructure_errors
from chef.infrastructure import paths as infrastructure_paths
from chef.mission import observatory as observatory
from chef.mission import state as mission_state
from chef.questions import records as question_records
from chef.slots import contracts as slots_contracts


def swap_file_link(match):
    before = match.string[match.start() - 1 : match.start()] if match.start() else ""
    path, _, anchor = match.group(1).partition("#")
    path = unquote(path)
    if not re.match(r"^[A-Za-z]:", path):
        path = "/" + path.lstrip("/")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm", *IMAGE_TYPES, *ASSET_TYPES):
        target = raw_url(path) + (f"#{anchor}" if anchor else "")
    else:
        target = view_url(path, anchor)
    if before == "(":
        return target
    if before in ("[", "`"):
        return match.group(0)
    return f"[{match.group(0)}]({target})"


def mission_folders(allowed, raw, real):
    found = set()
    absolute = os.path.abspath(raw)
    for location in allowed:
        try:
            entries = list(os.scandir(location.state_dir))
        except FileNotFoundError:
            continue
        for entry in entries:
            if not entry.is_dir() or not mission_guard.valid_mission_id(entry.name):
                continue
            folder = infrastructure_paths.request_realpath(entry.path)
            requested_inside = infrastructure_paths.inside_root(os.path.abspath(entry.path), absolute)
            if not requested_inside and not infrastructure_paths.inside_root(folder, real):
                continue
            if not mission_folder_allowed(location, entry.name):
                if requested_inside:
                    raise mission_state.StateError("미션 폴더 경로가 잘못됨")
                continue
            found.add(os.path.abspath(entry.path))
    return found


def read_allowed(config, raw):
    try:
        real = ""
        if raw and os.path.isabs(raw):
            real = infrastructure_paths.request_realpath(raw)
    except ValueError:
        real = ""
    allowed = locations(config)
    if not real or not any(
        infrastructure_paths.inside_root(infrastructure_paths.request_realpath(root), real)
        for location in allowed
        for root in (location.root, location.state_dir)
    ):
        return real, b"", message(403, "허용된 작업 공간·미션 경로 밖 경로")
    if not os.path.isfile(real):
        return real, b"", message(404, "파일 없음")
    try:
        folders = mission_folders(allowed, raw, real)
        if any(
            not document_visible(folder, raw, set())
            and not document_visible(folder, raw, mission_state.shared_documents(folder))
            for folder in folders
        ):
            return real, b"", message(404, "공유되지 않은 문서")
        with open(real, "rb") as f:
            data = f.read()
        if any(
            os.path.relpath(real, infrastructure_paths.request_realpath(folder)).replace(os.sep, "/")
            not in SYSTEM_DOCUMENTS
            and not mission_state.auto_question_document(
                folder, os.path.relpath(real, infrastructure_paths.request_realpath(folder))
            )
            and not shared_bytes_match(folder, real, data)
            for folder in folders
        ):
            return real, b"", message(404, "공유되지 않은 문서")
        return real, data, ()
    except (ValueError, mission_state.StateError):
        return real, b"", message(404, "공유되지 않은 문서")
    except OSError as e:
        return real, b"", message(500, f"읽기 실패: {e.strerror or e}")


def raw_type(real):
    ext = os.path.splitext(real)[1].lower()
    if ext in (".html", ".htm"):
        return HTML_TYPE
    if ext in IMAGE_TYPES:
        return IMAGE_TYPES[ext]
    if ext in ASSET_TYPES:
        return ASSET_TYPES[ext]
    guessed = mimetypes.guess_type(real)[0] or "application/octet-stream"
    return (
        f"{guessed}; charset=utf-8"
        if guessed.startswith("text/") or guessed.endswith(UTF8_SUFFIXES)
        else guessed
    )


@request_scoped
def raw_response(config, raw):
    real, data, failure = read_allowed(config, raw)
    return failure or (200, raw_type(real), data)


@request_scoped
def view_response(config, raw, embedded=False):
    real, data, failure = read_allowed(config, raw)
    if failure:
        return failure
    ext = os.path.splitext(real)[1].lower()
    if ext in IMAGE_TYPES:
        return 200, IMAGE_TYPES[ext], data
    if ext in (".html", ".htm"):
        return 200, HTML_TYPE, data
    if b"\0" in data[:BINARY_PROBE_BYTES]:
        return 200, "application/octet-stream", data
    if len(data) > MAX_TEXT_BYTES:
        return 200, TEXT_TYPE, data
    text = data.decode("utf-8-sig", errors="replace")
    title = os.path.basename(real)
    if ext == ".md":
        folder = os.path.dirname(real)
        if ("| id | 선택 | 설명 | 영향 |" in text or "| 문항 | 선택 id | 현재 선택 |" in text) and (
            os.path.isfile(os.path.join(folder, "mission.md"))
            and mission_state.auto_question_document(folder, title)
        ):
            try:
                record = question_records.checked_document(folder, title, text)
                text = question_records.render_document(record)
            except (question_records.QuestionRecordError, OSError):
                pass
        title = document_label(title, text, "", folder)
        boundaries = [
            item.root
            if infrastructure_paths.inside_root(infrastructure_paths.request_realpath(item.root), real)
            else item.state_dir
            for item in locations(config)
        ]
        root = max(
            (
                root
                for root in boundaries
                if infrastructure_paths.inside_root(infrastructure_paths.request_realpath(root), real)
            ),
            key=len,
            default=config.main,
        )
        source_json = json.dumps(FILE_URL.sub(swap_file_link, text)).replace("<", "\\u003c")
        body = (
            f'<p class="muted document-path">{esc(real)}</p><script type="application/json" id="source" data-url="{esc(raw_url(real))}" data-root="{esc(raw_url(root) + "/")}">{source_json}</script>'
            f'<article id="out" class="md"></article><script src="{MARKED_URL}"></script>{MARKDOWN_SCRIPT}'
        )
        return 200, HTML_TYPE, page(title, body, '<a href="/">← 개요</a>', embedded=embedded).encode("utf-8")
    lines = "".join(
        f'<div class="line" id="L{number}"><span class="no">{number}</span>{esc(line)}</div>'
        for number, line in enumerate(text.splitlines(), start=1)
    )
    body = f'<p class="muted">{esc(real)}</p><div class="code">{lines}</div>'
    return 200, HTML_TYPE, page(title, body, '<a href="/">← 개요</a>', embedded=embedded).encode("utf-8")


def observatory_response(config, format="html"):
    source = getattr(config, "observatory_report", None)
    if not source:
        return message(404, "실행 통계 보고서가 설정되지 않음")
    try:
        if os.path.getsize(source) > MAX_TEXT_BYTES:
            raise ValueError("report too large")
        with open(source, encoding="utf-8") as stream:
            report = observatory.normalize_report(json.load(stream))
    except (OSError, ValueError, TypeError, KeyError):
        return message(422, "실행 통계 보고서 형식·버전·무결성 확인 필요")
    if format == "json":
        return 200, "application/json; charset=utf-8", observatory.serialize_report(report).encode("utf-8")
    if format == "csv":
        return 200, "text/csv; charset=utf-8", observatory.serialize_report(report, "csv").encode("utf-8")
    body = '<p><a href="/">← 미션</a> · <a href="/run-metrics.json">JSON</a> · <a href="/run-metrics.csv">CSV</a></p>'
    body += "<p>파일에 기록된 관측만 표시합니다. unknown은 미관측이며 토큰은 과금·크레딧이 아닙니다.</p>"
    rows = report.get("byModel", [])
    peak = (
        max(
            [sum(row["tokens"].get(key) or 0 for key in ("input_tokens", "output_tokens")) for row in rows]
            or [1]
        )
        or 1
    )
    body += "<table><thead><tr><th>런타임 / 모델 / effort</th><th>관측 수</th><th>입력</th><th>출력</th><th>분포</th></tr></thead><tbody>"
    for row in rows:
        # Input/output only: total/cache/reasoning counters overlap and must not be added.
        amount = sum(row["tokens"].get(key) or 0 for key in ("input_tokens", "output_tokens"))
        body += (
            "<tr><td>"
            + esc(" / ".join(str(row.get(key, "unknown")) for key in ("runtime", "model", "effort")))
            + "</td>"
        )
        body += f"<td>{row['records']}</td>" + "".join(
            f"<td>{esc(row['tokens'].get(key) if row['tokens'].get(key) is not None else 'unknown')}</td>"
            for key in ("input_tokens", "output_tokens")
        )
        body += f'<td><meter min="0" max="{peak}" value="{amount}">{amount}</meter></td></tr>'
    body += "</tbody></table>"
    body += (
        "<h2>검증·리뷰 품질</h2><pre>"
        + esc(json.dumps(report.get("qualitySummary", {}), ensure_ascii=False, indent=2))
        + "</pre>"
    )
    body += (
        "<h2>관측 범위</h2><pre>"
        + esc(json.dumps(report.get("capabilities", {}), ensure_ascii=False, indent=2))
        + "</pre>"
    )
    return 200, HTML_TYPE, page("실행 통계", body).encode("utf-8")


def route(config, target):
    parsed = urlsplit(target)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    if parsed.path in (
        "/run-metrics",
        "/run-metrics.json",
        "/run-metrics.csv",
        "/observatory",
        "/observatory.json",
        "/observatory.csv",
    ):
        return observatory_response(config, parsed.path.rsplit(".", 1)[-1] if "." in parsed.path else "html")
    if parsed.path == "/":
        return overview_page(config)
    if parsed.path == "/mission":
        return mission_page(config, query.get("slot", ""), query.get("id", ""))
    if parsed.path == "/view":
        return view_response(config, query.get("path", ""), embedded=query.get("embedded", "") == "1")
    if parsed.path.startswith(RAW_PREFIX):
        raw = unquote(parsed.path[len(RAW_PREFIX) :])
        if os.name != "nt":
            raw = "/" + raw.lstrip("/")
        return raw_response(config, raw)
    return message(404, "없는 주소")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        redirect = ""
        try:
            status, content_type, body = route(self.server.config, self.path)
            parsed = urlsplit(self.path)
            path = parse_qs(parsed.query).get("path", [""])[0]
            if (
                status == 200
                and parsed.path == "/view"
                and os.path.splitext(path)[1].lower() in (".html", ".htm")
            ):
                redirect = raw_url(path)
                status, body = 302, b""
        except Exception as e:  # 요청 하나가 터져도 서버는 계속 돌아야 함
            status, content_type, body = 500, TEXT_TYPE, f"내부 오류: {e}".encode("utf-8")
        self.send_response(status)
        if redirect:
            self.send_header("Location", redirect)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Server(ThreadingHTTPServer):
    # Windows 는 SO_REUSEADDR 로 쓰는 중인 포트에도 바인드돼 버림
    allow_reuse_address = False

    def __init__(self, config, port):
        self.config = config
        super().__init__(("127.0.0.1", port), Handler)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dashboard.py", description="미션 상태·질문 대시보드 (읽기 전용 로컬 서버)"
    )
    parser.add_argument("--runtime", choices=slots_contracts.RUNTIMES)
    parser.add_argument("--project", help="어댑터를 찾기 시작할 폴더 (기본: 현재 폴더)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="브라우저로 열기")
    parser.add_argument(
        "--run-metrics-report",
        "--observatory-report",
        dest="observatory_report",
        help="명시한 로컬 실행 통계 보고서만 읽기 전용 표시",
    )
    return parser


def main(argv=None, runtime="claude"):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return slots_contracts.USAGE if e.code else slots_contracts.OK
    runtime = args.runtime or runtime
    try:
        located = infrastructure_configuration.locate(args.project, runtime)
        config = dashboard_discovery.Config(
            located, infrastructure_configuration.read_adapter(located.adapter_path, runtime), runtime
        )
        config.observatory_report = args.observatory_report
    except (
        infrastructure_errors.UsageError,
        mission_guard.GuardOff,
        ValueError,
        TypeError,
        AttributeError,
    ) as e:
        print(f"사용 오류: {e}", file=sys.stderr)
        return slots_contracts.USAGE
    try:
        server = Server(config, args.port)
    except OSError as e:
        print(f"사용 오류: 포트 {args.port} 를 열 수 없음 ({e.strerror or e})", file=sys.stderr)
        return slots_contracts.USAGE
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"대시보드: {url} (Ctrl+C 로 끝냄)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return slots_contracts.OK


if __name__ == "__main__":
    sys.exit(main())
