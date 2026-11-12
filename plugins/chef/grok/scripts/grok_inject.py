#!/usr/bin/env python3
"""Grok 세션에 답을 넣는다. 세션이 쉬고 있을 때만 넣는다.

- 슬롯 TUI 가 리더 모드(`grok --leader --leader-socket <sock>`)로 떠 있으면 같은 리더에 ACP 클라이언트로 붙어
  session/load → session/prompt 로 넣는다. 열린 TUI 화면에 새 턴이 그대로 보인다.
- 살아 있는 리더가 없으면 `grok --cwd <dir> -r <id> -p <text>` 로 넣는다. 이때 그 세션의 TUI 가 열려 있으면 거부한다
  (리더 없이 열린 창은 디스크 기록을 다시 읽지 않아 화면이 안 바뀌고, 다음 입력이 기록을 덮을 수 있음).
- 턴이 도는 중에 넣으면 follow_up_behavior=steer 가 진행 중 턴을 가로채므로, 쉬는 중이 아니면 거부한다.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grok_transcript  # noqa: E402

OK = 0
FAILED = 1
USAGE = 2
BUSY = 3

CONFIRM_SECONDS = 90
PROMPT_SECONDS = 12 * 3600
DROPPED_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "TERM", "MSYSTEM", "SHELL", "NO_COLOR", "FORCE_COLOR", "_ZO_DOCTOR", "GIT_TERMINAL_PROMPT",
)


class InjectError(Exception):
    pass


def grok_exe():
    configured = os.environ.get("GROK_BIN")
    if configured:
        return configured
    found = shutil.which("grok")
    if found:
        return found
    return os.path.join(grok_transcript.grok_home(), "bin", "grok.exe" if os.name == "nt" else "grok")


def clean_env():
    return {key: value for key, value in os.environ.items() if key.upper() not in DROPPED_ENV}


def same_dir(first, second):
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))


# --- 프로세스 -----------------------------------------------------------------

def grok_processes():
    """{pid: commandLine} — 이름이 grok(.exe) 인 프로세스."""
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process -Filter \"Name='grok.exe'\" | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise InjectError(f"grok 프로세스 목록 조회 실패: {exc}") from exc
        if not out:
            return {}
        try:
            rows = json.loads(out)
        except ValueError as exc:
            raise InjectError(f"grok 프로세스 목록을 못 읽음: {out[:200]}") from exc
        rows = rows if isinstance(rows, list) else [rows]
        return {int(row["ProcessId"]): row.get("CommandLine") or "" for row in rows if isinstance(row, dict)}
    result = {}
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise InjectError(f"프로세스 목록 조회 실패: {exc}") from exc
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and os.path.basename(args.split(" ", 1)[0]) in ("grok", "grok.exe"):
            result[int(pid)] = args
    return result


def lock_path(socket):
    base = socket[:-5] if socket.endswith(".sock") else socket
    return base + ".lock"


def leader_pid(socket, processes):
    """리더 소켓의 잠금 파일에 적힌 PID 가 살아 있는 `grok agent leader` 면 그 PID, 아니면 0."""
    if not socket:
        return 0
    try:
        with open(lock_path(socket), encoding="utf-8-sig") as handle:
            text = handle.read().strip()
    except OSError:
        return 0
    if not text.isdigit():
        return 0
    pid = int(text)
    command = processes.get(pid, "")
    return pid if "agent" in command and "leader" in command else 0


def open_tuis(session, processes, home=""):
    """그 세션을 연 grok 대화 창(TUI) PID 목록. active_sessions.json 과 명령줄 둘 다 본다."""
    pids = set()
    path = os.path.join(grok_transcript.grok_home(home), "active_sessions.json")
    try:
        with open(path, encoding="utf-8-sig") as handle:
            rows = json.load(handle)
    except (OSError, ValueError):
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("session_id") == session and isinstance(row.get("pid"), int):
            if row["pid"] in processes:
                pids.add(row["pid"])
    for pid, command in processes.items():
        if session in command and " agent " not in f" {command} ":
            pids.add(pid)
    return sorted(pids)


# --- 세션 -------------------------------------------------------------------

def session_files(session, home=""):
    path = grok_transcript.find_transcript(session, home)
    if not path:
        raise InjectError(f"Grok 세션 {session} 기록(updates.jsonl)을 못 찾음")
    return path


def session_cwd(path):
    try:
        with open(os.path.join(os.path.dirname(path), "summary.json"), encoding="utf-8-sig") as handle:
            info = json.load(handle).get("info") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    return info.get("cwd") or ""


def wait_idle(path, wait, timeout, poll, home="", out=print):
    deadline = time.time() + timeout
    while True:
        reasons = grok_transcript.busy_reasons(path, home)
        if not reasons or not wait or time.time() >= deadline:
            return reasons
        out(f"대기: 쉬는 중이 아님 — {'; '.join(reasons)} ({poll}초 뒤 다시 봄)")
        time.sleep(poll)


def user_turn_since(path, offset):
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return False
    return b'"user_message_chunk"' in data


# --- 실행 -------------------------------------------------------------------

def detach_flags():
    if os.name != "nt":
        return {"start_new_session": True}
    return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x01000000}


def spawn_detached(argv, log, env):
    with open(log, "ab") as stream:
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, env=env, **detach_flags())
        except OSError:
            if os.name != "nt":
                raise
            # 부모 작업 개체가 breakaway 를 막으면 그 플래그 없이 띄움
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, env=env, creationflags=flags)


def inject(cwd, session, text, socket="", log_dir="", wait=False, timeout=1800, poll=15, dry_run=False,
           home="", env=None, out=print, confirm_seconds=CONFIRM_SECONDS):
    if not grok_transcript.SESSION_ID.fullmatch(session or ""):
        raise InjectError(f"세션 id 형식 오류: {session!r}")
    if not text.strip():
        raise InjectError("넣을 내용이 비어 있음")
    path = session_files(session, home)
    recorded = session_cwd(path)
    if recorded and not same_dir(recorded, cwd):
        raise InjectError(f"세션 {session} 의 cwd 는 {recorded} — {cwd} 가 아님")
    reasons = wait_idle(path, wait, timeout, poll, home, out)
    if reasons:
        out("거부: 세션이 쉬는 중이 아님 — " + "; ".join(reasons))
        return BUSY
    processes = grok_processes()
    leader = leader_pid(socket, processes)
    tuis = open_tuis(session, processes, home)
    if leader:
        route = "leader"
        out(f"경로: 리더 {leader} ({socket}) 에 ACP 로 넣음 — 열린 TUI {tuis or '없음'}")
    elif tuis:
        out(f"거부: 세션 {session} 의 TUI 가 리더 없이 열려 있음 (pid {', '.join(map(str, tuis))}) — "
            "-p 로 넣으면 화면이 안 바뀌고 다음 입력이 기록을 덮을 수 있음. 창을 닫거나 slots.py run --resume 으로 리더 모드로 다시 열 것")
        return FAILED
    else:
        route = "headless"
        out("경로: 살아 있는 리더·열린 TUI 없음 — grok -r -p 로 넣음")
    if dry_run:
        out("dry-run: 넣지 않음")
        return OK
    log_dir = log_dir or os.path.dirname(path)
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(log_dir, f"inject-{session[:8]}-{stamp}")
    with open(base + ".txt", "w", encoding="utf-8") as handle:
        handle.write(text)
    env = dict(env or clean_env())
    # 띄우기 직전 다시 확인 — 그 사이 사용자가 TUI 에 입력했을 수 있음
    reasons = grok_transcript.busy_reasons(path, home)
    if reasons:
        out("거부: 넣기 직전 다시 보니 쉬는 중이 아님 — " + "; ".join(reasons))
        return BUSY
    offset = os.path.getsize(path)
    if route == "leader":
        argv = [sys.executable, os.path.abspath(__file__), "acp", "--socket", socket, "--cwd", cwd,
                "--session", session, "--text-file", base + ".txt"]
    else:
        argv = [grok_exe(), "--cwd", cwd, "-r", session, "-p", text]
    process = spawn_detached(argv, base + ".log", env)
    out(f"보냄: pid {process.pid}, 로그 {base}.log")
    deadline = time.time() + confirm_seconds
    while time.time() < deadline:
        if user_turn_since(path, offset):
            out(f"요약: 세션 {session} 에 새 턴이 시작됨 ({route})")
            return OK
        if process.poll() is not None and not user_turn_since(path, offset):
            out(f"실패: 넣는 프로세스가 끝났는데 새 턴이 기록되지 않음 (종료 코드 {process.returncode}) — {base}.log")
            return FAILED
        time.sleep(1)
    out(f"실패: {confirm_seconds}초 안에 새 턴 기록이 안 보임 — {base}.log 확인 (프로세스 {process.pid} 는 그대로 둠)")
    return FAILED


# --- ACP 클라이언트 ----------------------------------------------------------

class AcpClient:
    def __init__(self, socket, log):
        self.log = log
        argv = [grok_exe(), "agent", "--leader", "--leader-socket", socket, "--always-approve", "stdio"]
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
        self.pending = {}
        self.lock = threading.Lock()
        self.next_id = 1
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()

    def send(self, payload):
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def read_stderr(self):
        for line in self.process.stderr:
            self.log("STDERR " + line.decode("utf-8", errors="replace").rstrip())

    def read_stdout(self):
        for raw in self.process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.log("PARSE " + line[:300])
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                self.answer(message)
            elif "method" in message:
                update = (message.get("params") or {}).get("update") or {}
                kind = update.get("sessionUpdate") if isinstance(update, dict) else ""
                if kind in ("turn_completed", "user_message_chunk"):
                    self.log("UPDATE " + str(kind))
            elif message.get("id") in self.pending:
                slot = self.pending.pop(message["id"])
                slot["message"] = message
                slot["event"].set()
        for slot in list(self.pending.values()):
            slot["event"].set()

    def answer(self, message):
        method = message.get("method")
        if method == "session/request_permission":
            options = (message.get("params") or {}).get("options") or []
            chosen = next((option for option in options if str(option.get("kind", "")).startswith("allow")), options[0] if options else None)
            result = {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}} if chosen else {"outcome": {"outcome": "cancelled"}}
            self.log(f"PERMISSION {json.dumps(result)}")
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            return
        self.log(f"REQUEST {method} -> method not found")
        self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"{method} not supported"}})

    def request(self, method, params, timeout):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        slot = {"event": threading.Event(), "message": None}
        self.pending[request_id] = slot
        self.log(f"SEND {method}")
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self.pending.pop(request_id, None)
            raise InjectError(f"{method} 응답 없음 ({timeout}초)")
        message = slot["message"]
        if message is None:
            raise InjectError(f"{method} 응답 전에 grok agent 가 끝남")
        if message.get("error"):
            raise InjectError(f"{method} 오류: {json.dumps(message['error'], ensure_ascii=False)[:500]}")
        return message.get("result") or {}

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def run_acp(socket, cwd, session, text, prompt_seconds=PROMPT_SECONDS):
    def log(message):
        print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)

    client = AcpClient(socket, log)
    try:
        init = client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, 60)
        methods = [method.get("id") for method in init.get("authMethods") or [] if isinstance(method, dict)]
        method = "cached_token" if "cached_token" in methods else (methods[0] if methods else "")
        if not method:
            raise InjectError("인증 방법이 없음")
        client.request("authenticate", {"methodId": method, "_meta": {"headless": True}}, 60)
        log("AUTH_OK " + method)
        client.request("session/load", {"sessionId": session, "cwd": cwd, "mcpServers": []}, 120)
        log("LOAD_OK")
        result = client.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": text}]}, prompt_seconds)
        log("PROMPT_DONE " + json.dumps(result, ensure_ascii=False)[:300])
        return OK
    except InjectError as exc:
        log(f"FATAL {exc}")
        return FAILED
    finally:
        client.close()


# --- CLI --------------------------------------------------------------------

def read_text(args):
    if args.file:
        with open(args.file, encoding="utf-8-sig") as handle:
            return handle.read()
    return args.text or ""


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="grok_inject.py", description="쉬는 중인 Grok 세션에만 답을 넣음")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--cwd", required=True)
    send.add_argument("--session", required=True)
    send.add_argument("--socket", default="")
    group = send.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--file")
    send.add_argument("--log-dir", default="")
    send.add_argument("--wait", action="store_true")
    send.add_argument("--timeout", type=int, default=1800)
    send.add_argument("--poll", type=int, default=15)
    send.add_argument("--dry-run", action="store_true")
    idle = sub.add_parser("idle")
    idle.add_argument("--session", required=True)
    acp = sub.add_parser("acp")
    acp.add_argument("--socket", required=True)
    acp.add_argument("--cwd", required=True)
    acp.add_argument("--session", required=True)
    acp.add_argument("--text-file", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return USAGE if exc.code else OK
    try:
        if args.command == "acp":
            with open(args.text_file, encoding="utf-8") as handle:
                return run_acp(args.socket, args.cwd, args.session, handle.read())
        if args.command == "idle":
            reasons = grok_transcript.busy_reasons(session_files(args.session))
            print("쉬는 중" if not reasons else "쉬는 중 아님 — " + "; ".join(reasons))
            return OK if not reasons else BUSY
        return inject(args.cwd, args.session, read_text(args), args.socket, args.log_dir, args.wait, args.timeout, args.poll, args.dry_run)
    except InjectError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
