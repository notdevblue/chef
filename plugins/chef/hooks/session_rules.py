import json
import os
import sys
from hook_runtime import normalize_payload

ENV_FILE_ENV = "CLAUDE_ENV_FILE"
USER_CONFIG_ENV = "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR"
REGISTRY_ENV = "HAN_ADAPTERS_DIR"
TRANSCRIPT_ENV = "HAN_TRANSCRIPT"
SESSION_START = "SessionStart"
SHELL_ESCAPED = ('"', "$", "`")


def read_payload():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def export_line(name, value):
    escaped = value.replace("\\", "/")
    for char in SHELL_ESCAPED:
        escaped = escaped.replace(char, "\\" + char)
    return f'export {name}="{escaped}"'


def has_line(path, line):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return line in f.read().splitlines()
    except FileNotFoundError:
        return False


def export_to_env_file(env_file, name, value, consequence):
    line = export_line(name, value)
    try:
        if has_line(env_file, line):
            return ""
        with open(env_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError as e:
        return f"{name} 를 {ENV_FILE_ENV} 에 쓰지 못함 ({consequence}): {e}"
    return ""


def export_registry_dir():
    env_file = os.environ.get(ENV_FILE_ENV)
    configured = os.environ.get(USER_CONFIG_ENV, "").strip()
    if not env_file or not configured or os.environ.get(REGISTRY_ENV):
        return ""
    return export_to_env_file(env_file, REGISTRY_ENV, configured, "Bash 도구에서 어댑터 레지스트리를 못 찾을 수 있음")


def export_transcript_path(payload):
    env_file = os.environ.get(ENV_FILE_ENV)
    transcript = payload.get("transcript_path")
    if not env_file or not isinstance(transcript, str) or not transcript.strip():
        return ""
    return export_to_env_file(env_file, TRANSCRIPT_ENV, transcript.strip(), "Bash 도구에서 대화 기록 경로를 못 찾을 수 있음")


def render_output(event, context, warnings):
    output = {}
    if warnings:
        output["systemMessage"] = " / ".join(warnings)
    if context:
        output["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    return output


def decide(payload, runtime=""):
    payload = normalize_payload(payload, runtime)
    runtime = payload["han_runtime"]
    event = payload.get("hook_event_name", SESSION_START)
    if event == SESSION_START:
        warnings = []
        contexts = []
    else:
        return {}
    if runtime == "claude":
        for export_warning in (export_registry_dir(), export_transcript_path(payload)):
            if export_warning:
                warnings.append(export_warning)
    if runtime in ("codex", "grok"):
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript.strip():
            contexts.append(f"chef-distill 대화 기록 경로 (--transcript): {json.dumps(transcript.strip(), ensure_ascii=False)}")
    return render_output(event, "\n\n".join(filter(None, contexts)), warnings)


def main(runtime=""):
    try:
        output = decide(read_payload(), runtime)
    except Exception as e:  # 훅 실패로 세션을 막지 않음
        output = {"systemMessage": f"세션 경로 전달 실패: {type(e).__name__}: {e}"}
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=True))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
