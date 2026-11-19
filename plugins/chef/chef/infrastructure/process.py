"""infrastructure / process."""

import os

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
    dropped = PARENT_SESSION_ENV + AGENT_SHELL_ENV + AGENT_TOOL_ENV
    return {key: value for key, value in os.environ.items() if key.upper() not in dropped}


def child_env(cwd):
    # p4 는 실제 cwd 보다 PWD 로 .p4config 를 찾음, 부모 PWD 가 남으면 레지스트리 P4CLIENT 로 떨어짐
    return {**os.environ, "PWD": cwd or os.getcwd()}


def decode(data):
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")
