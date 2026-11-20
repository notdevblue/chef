"""infrastructure / process."""

import os
import shutil

from chef.infrastructure.errors import UsageError


def default_p4_installations():
    """Known install locations, including user-local bins used by launchd/gateway PATHs."""
    if os.name == "nt":
        return (
            r"C:\Program Files\Perforce\p4.exe",
            r"C:\Program Files (x86)\Perforce\p4.exe",
        )
    home = os.path.expanduser("~")
    return (
        "/opt/homebrew/bin/p4",
        "/usr/local/bin/p4",
        "/opt/perforce/bin/p4",
        "/usr/bin/p4",
        os.path.join(home, ".local", "bin", "p4"),
        os.path.join(home, "bin", "p4"),
    )


P4_INSTALLATIONS = default_p4_installations()

PARENT_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
    "AI_AGENT",
    "TRACEPARENT",
    "HAN_TRANSCRIPT",
)


AGENT_SHELL_ENV = ("TERM", "MSYSTEM", "SHELL")


AGENT_TOOL_ENV = ("NO_COLOR", "FORCE_COLOR", "_ZO_DOCTOR", "GIT_TERMINAL_PROMPT")


def launch_env():
    # 부모 세션 표식이 남으면 새 claude 가 자식 세션으로 보고 대화 기록을 안 남김
    # 에이전트 셸(git bash) 단말 표식이 남으면 새 탭 claude 가 mintty 로 보고 화면을 다르게 그림
    # 에이전트 PowerShell·Grok 셸 도구가 넣은 값이 남으면 새 탭 claude 가 흑백으로 뜨고 git 인증을 못 물음
    dropped = PARENT_SESSION_ENV + AGENT_TOOL_ENV + ("MSYSTEM",)
    if os.name == "nt":
        dropped += AGENT_SHELL_ENV
    return {key: value for key, value in os.environ.items() if key.upper() not in dropped}


def tool_search_path(p4_executable=None):
    original = os.environ.get("PATH", os.defpath)
    entries = original.split(os.pathsep) if original else []
    seen = {os.path.normcase(os.path.normpath(entry)) for entry in entries}
    candidates = [os.path.dirname(path) for path in P4_INSTALLATIONS]
    if p4_executable:
        candidates.insert(0, os.path.dirname(p4_executable))
    added = []
    for directory in candidates:
        key = os.path.normcase(os.path.normpath(directory))
        if key not in seen and os.path.isdir(directory):
            added.append(directory)
            seen.add(key)
    return os.pathsep.join([*added, *entries])


def resolve_p4(configured=None, *, cwd=None):
    search_path = tool_search_path()
    hint = "Set slots.p4, P4_BIN, or CHEF_P4 to the absolute path to the p4 executable."

    def executable_file(path):
        return os.path.isfile(path) and os.access(path, os.X_OK)

    if configured:
        if os.path.isabs(configured):
            if executable_file(configured):
                return configured
            raise UsageError(f"Perforce CLI not found at slots.p4: {configured!r}. {hint}")
        target = os.path.join(cwd, configured) if cwd and os.path.dirname(configured) else configured
        found = shutil.which(target, path=search_path)
        if found:
            return os.path.abspath(found)
        raise UsageError(f"Perforce CLI from slots.p4 ({configured!r}) not found on PATH ({search_path}). {hint}")

    for name in ("P4_BIN", "CHEF_P4"):
        path = os.environ.get(name)
        if path:
            if os.path.isabs(path) and executable_file(path):
                return path
            raise UsageError(f"{name}={path!r} must be an absolute path to an executable file. {hint}")

    found = shutil.which("p4", path=search_path)
    if found:
        return os.path.abspath(found)
    for path in P4_INSTALLATIONS:
        if executable_file(path):
            return path
    attempted = ", ".join(P4_INSTALLATIONS)
    raise UsageError(f"Perforce CLI (p4) not found. Searched PATH ({search_path}) and {attempted}. {hint}")


def child_env(cwd, p4_executable=None):
    # p4 는 실제 cwd 보다 PWD 로 .p4config 를 찾음, 부모 PWD 가 남으면 레지스트리 P4CLIENT 로 떨어짐
    return {**os.environ, "PWD": cwd or os.getcwd(), "PATH": tool_search_path(p4_executable)}


def decode(data):
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")
