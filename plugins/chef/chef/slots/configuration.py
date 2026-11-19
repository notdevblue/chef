"""slots / configuration."""

import json
import os
import re
import sys

from chef.guards import mission as mission_guard
from chef.infrastructure.errors import UsageError
from chef.infrastructure.paths import inside_real_root, is_link, normalize, same_path
from chef.infrastructure.vcs import build_vcs
from chef.runtimes.claude.session import claude_memory_dir
from chef.locations import PLUGIN_ROOT
from chef.slots.contracts import NAME_PATTERN, SYNC_STEP
from chef.slots.models import Step

VARIABLE = re.compile(r"\{(\w+)\}")


CODEX_AGENT_NAME = re.compile(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?")


TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


CODEX_AGENT_RESERVED = {
    "max_threads",
    "max_depth",
    "job_max_runtime_seconds",
    "default_subagent_model",
    "default_subagent_reasoning_effort",
    "enabled",
    "interrupt_message",
    "max_concurrent_threads_per_session",
}


def expand(template, values):
    missing = sorted({key for key in VARIABLE.findall(template) if key not in values})
    if missing:
        raise UsageError(f"정의 안 된 변수 {', '.join('{' + key + '}' for key in missing)}: {template}")
    return VARIABLE.sub(lambda m: values[m.group(1)], template)


def toml_string(value):
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise UsageError("Codex 역할 설정 문자열은 유효한 UTF-8이어야 함") from exc
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f").replace(";", "\\u003b")


def toml_key(alias):
    if TOML_BARE_KEY.fullmatch(alias):
        return alias
    return toml_string(alias)


class Config:
    def __init__(self, located, adapter, runtime="claude"):
        self.runtime = runtime
        slots = adapter.get("slots")
        if not isinstance(slots, dict) or not slots.get("enabled"):
            raise UsageError("어댑터에 slots 설정이 없거나 enabled 가 아님")
        for key in ("root", "client"):
            if not isinstance(slots.get(key), str) or not slots[key]:
                raise UsageError(f"slots.{key} 가 없음 (local.json 확인)")
        self.vars = {key: str(value) for key, value in (adapter.get("vars") or {}).items()}
        self.project_name = str(adapter.get("project") or os.path.basename(located.project))
        self.main = normalize(self.vars.get("main") or located.project)
        self.state_rel = (adapter.get("paths") or {}).get("stateDir")
        if not isinstance(self.state_rel, str) or not self.state_rel:
            raise UsageError("adapter.json 에 paths.stateDir 없음")
        self.state_dir = normalize(os.path.join(self.main, self.state_rel))
        self.root = slots["root"]
        self.client_template = slots["client"]
        self.stale_hours = mission_guard.stale_hours_of(adapter)
        self.names = list(dict.fromkeys(slots.get("names") or []))
        for name in self.names:
            self.check_format(name)
        self.prepare = list(slots.get("prepare") or [])
        self.before_run = slots.get("beforeRun", [])
        if not isinstance(self.before_run, list):
            raise UsageError("slots.beforeRun 은 단계 배열이어야 함")
        self.refresh_ids = list(slots.get("refresh") or [])
        self.editor = slots.get("editor") or ""
        if not isinstance(self.editor, str) or "\x00" in self.editor:
            raise UsageError("slots.editor 는 NUL 없는 문자열이어야 함")
        self.editor_argv = slots.get("editorArgv", [])
        if "editorArgv" in slots and (
            not isinstance(self.editor_argv, list) or not self.editor_argv
            or any(not isinstance(value, str) or "\x00" in value for value in self.editor_argv)
            or not self.editor_argv[0]
        ):
            raise UsageError("slots.editorArgv 는 비어 있지 않은 NUL 없는 문자열 배열이어야 함")
        if self.editor and self.editor_argv:
            raise UsageError("slots.editor 와 slots.editorArgv 는 함께 지정할 수 없음")
        self.requirements = slots.get("requirements", {})
        if not isinstance(self.requirements, dict) or set(self.requirements) - {"executables", "files"}:
            raise UsageError("slots.requirements 에 executables·files 배열만 지정할 수 있음")
        for key, values in self.requirements.items():
            if not isinstance(values, list) or any(not isinstance(value, str) or not value or "\x00" in value for value in values):
                raise UsageError("slots.requirements." + key + " 는 비어 있지 않은 경로 문자열 배열이어야 함")
        self.terminal = slots.get("terminal", "auto")
        if self.terminal not in ("auto", "foreground", "wt", "macos-terminal", "gnome-terminal", "konsole", "tmux"):
            raise UsageError("slots.terminal 의 실행 방식이 지원되지 않음")
        self.runtime_arguments = slots.get("runtimeArguments", [])
        if not isinstance(self.runtime_arguments, list):
            raise UsageError("slots.runtimeArguments 는 문자열 배열이어야 함")
        for index, argument in enumerate(self.runtime_arguments):
            self.check_runtime_argument(argument, index)
        default_autocompact = 200000
        if runtime == "claude":
            default_autocompact = 500000
        self.autocompact = slots.get("autocompact", default_autocompact)
        if type(self.autocompact) is not int or not 100000 <= self.autocompact <= 1000000:
            raise UsageError("slots.autocompact 는 100000~1000000 정수여야 함")
        self.codex_sandbox_mode = ""
        if runtime == "codex" and "sandboxMode" in slots:
            sandbox_mode = slots["sandboxMode"]
            if not isinstance(sandbox_mode, str) or sandbox_mode not in (
                "read-only",
                "workspace-write",
                "danger-full-access",
            ):
                raise UsageError(
                    "slots.sandboxMode 는 read-only·workspace-write·danger-full-access 중 하나여야 함"
                )
            self.codex_sandbox_mode = sandbox_mode
        self.codex_project_doc_max_bytes = 0
        if "codexProjectDocMaxBytes" in slots:
            limit = slots["codexProjectDocMaxBytes"]
            if type(limit) is not int or limit <= 0:
                raise UsageError("slots.codexProjectDocMaxBytes 는 양의 정수여야 함")
            self.codex_project_doc_max_bytes = limit
        self.codex_agent_configs = slots.get("codexAgentConfigs", {})
        if not isinstance(self.codex_agent_configs, dict):
            raise UsageError("slots.codexAgentConfigs 는 역할 이름별 객체여야 함")
        for alias, role in self.codex_agent_configs.items():
            if (
                not isinstance(alias, str)
                or not CODEX_AGENT_NAME.fullmatch(alias)
                or alias in CODEX_AGENT_RESERVED
            ):
                raise UsageError(
                    f"slots.codexAgentConfigs 의 역할 이름이 잘못되었거나 예약된 키임: {alias!r}"
                )
            if not isinstance(role, dict) or set(role) != {"configFile", "description"}:
                raise UsageError(f"slots.codexAgentConfigs.{alias} 에 configFile·description 만 필요함")
            for key, value in role.items():
                if not isinstance(value, str) or not value.strip():
                    raise UsageError(
                        f"slots.codexAgentConfigs.{alias}.{key} 는 비어 있지 않은 문자열이어야 함"
                    )
                toml_string(value)
        self.stages = [
            stage for stage in adapter.get("stages") or [] if isinstance(stage, dict) and stage.get("id")
        ]
        self.vcs = build_vcs(slots, self.main)
        self.baseline = ""
        self.baseline_origin = ""
        self.rows_path = os.path.join(self.state_dir, "slots.tsv")
        self.exclusive_lock_path = os.path.join(self.state_dir, "slots", "exclusive.lock")
        self.main_lock_path = os.path.join(self.state_dir, "slots", "main.lock")

    def slot_path(self, name):
        return normalize(os.path.join(self.root, name))

    @staticmethod
    def check_runtime_argument(argument, index):
        if not isinstance(argument, str) or any(char in argument for char in "\x00\r\n"):
            raise UsageError(f"slots.runtimeArguments[{index}] 는 NUL·개행 없는 문자열이어야 함")
        try:
            argument.encode("utf-8")
        except UnicodeError as exc:
            raise UsageError(f"slots.runtimeArguments[{index}] 는 유효한 UTF-8이어야 함") from exc

    def runtime_arguments_for(self, name):
        arguments = []
        for index, template in enumerate(self.runtime_arguments):
            argument = expand(template, self.values(name))
            self.check_runtime_argument(argument, index)
            arguments.append(argument)
        return arguments

    def codex_agent_entries(self, name):
        entries = []
        for alias, role in self.codex_agent_configs.items():
            path = expand(role["configFile"], self.values(name))
            if not os.path.isabs(path):
                raise UsageError(
                    f"slots.codexAgentConfigs.{alias}.configFile 은 확장 후 절대 경로여야 함: {path}"
                )
            entries.append((alias, normalize(path), role["description"]))
        return entries

    def check_codex_agent_files(self, name):
        if self.runtime == "codex":
            for alias, path, _ in self.codex_agent_entries(name):
                if not os.path.isfile(path):
                    raise UsageError(f"slots.codexAgentConfigs.{alias}.configFile 파일이 없음: {path}")
                try:
                    with open(path, "rb"):
                        pass
                except OSError as exc:
                    raise UsageError(
                        f"slots.codexAgentConfigs.{alias}.configFile 파일을 읽을 수 없음: {path}: {exc}"
                    ) from exc

    def client_of(self, name):
        return self.client_template.replace("{name}", name)

    def slot_state_dir(self, name):
        return normalize(os.path.join(self.slot_path(name), self.state_rel))

    def slot_lock_path(self, name):
        return normalize(os.path.join(self.state_dir, "slots", f"{name}.lock"))

    def log_path(self, name, step_id, suffix=".log", namespace=""):
        return normalize(os.path.join(self.state_dir, "slots", namespace, f"{name}-{step_id}{suffix}"))

    def timing_path(self, name, namespace=""):
        return normalize(os.path.join(self.state_dir, "slots", namespace, f"{name}-timing.tsv"))

    def values(self, name, step_id="", namespace=""):
        values = dict(self.vars)
        values.update(
            name=name,
            slot=self.slot_path(name),
            client=self.client_of(name),
            main=self.main,
            python=sys.executable,
            plugin=str(PLUGIN_ROOT),
        )
        if self.runtime == "claude":
            values.update(
                memoryDir=claude_memory_dir(self.slot_path(name)), mainMemoryDir=claude_memory_dir(self.main)
            )
        if step_id:
            values["log"] = self.log_path(name, step_id, namespace=namespace)
        return values

    def prepare_entries(self):
        return self.step_entries(self.prepare, "prepare")

    def check_requirements(self, name):
        from chef.slots.preparation import PreparationError, preflight

        values = self.values(name)
        try:
            preflight(
                [expand(value, values) for value in self.requirements.get("executables", [])],
                [expand(value, values) for value in self.requirements.get("files", [])],
                cwd=self.main,
            )
        except (PreparationError, OSError) as exc:
            raise UsageError(str(exc)) from exc

    def step_entries(self, raw_steps, source):
        entries = []
        for index, raw in enumerate(raw_steps):
            step_id = ""
            if isinstance(raw, dict):
                step_id = str(raw.get("id") or "")
            if not step_id:
                raise UsageError(f"slots.{source}[{index}] 에 id 가 필요함")
            if ("run" in raw) == ("argv" in raw):
                raise UsageError(f"slots.{source}[{index}] 에 run 문자열 또는 argv 배열 중 하나만 필요함")
            if "run" in raw and (not isinstance(raw["run"], str) or not raw["run"]):
                raise UsageError(f"slots.{source}[{index}] 의 run 은 비어 있지 않은 문자열이어야 함")
            if "argv" in raw:
                argv = raw["argv"]
                if (
                    not isinstance(argv, list)
                    or not argv
                    or any(not isinstance(argument, str) or "\x00" in argument for argument in argv)
                    or not argv[0]
                ):
                    raise UsageError(f"slots.{source}[{index}] 의 argv 는 실행 파일로 시작하는 NUL 없는 문자열 배열이어야 함")
            if not isinstance(raw.get("cwd") or "", str):
                raise UsageError(f"slots.{source}[{index}] ({step_id}) 의 cwd 가 문자열이 아님")
            try:
                group = int(raw.get("group", 0))
            except (TypeError, ValueError):
                raise UsageError(
                    f"slots.{source}[{index}] ({step_id}) 의 group 이 숫자가 아님: {raw.get('group')!r}"
                )
            entries.append((step_id, group, raw))
        return entries

    def build_steps(self, name, only=None):
        entries = self.prepare_entries()
        if only is not None:
            entries = [entry for entry in entries if entry[0] in only]
        return self.convert_steps(name, entries)

    def before_run_steps(self, name, launch_mode, session_id):
        values = {
            "launchMode": launch_mode,
            "sessionId": session_id,
            "slotLockPath": os.path.abspath(self.slot_lock_path(name)),
            "slotLockOwnerPid": str(os.getpid()),
        }
        return self.convert_steps(name, self.step_entries(self.before_run, "beforeRun"), "beforeRun", values)

    def convert_steps(self, name, entries, namespace="", extra_values=()):
        steps = []
        for step_id, group, raw in entries:
            if namespace:
                step_id = f"{namespace}.{step_id}"
            values = self.values(name, step_id, namespace)
            values.update(extra_values)
            templates = raw["argv"] if "argv" in raw else [raw["run"]]
            if any("{baseline}" in template for template in [*templates, raw.get("cwd") or ""]):
                if not self.baseline:
                    self.baseline = self.vcs.baseline()
                values["baseline"] = self.baseline
            cwd = ""
            if raw.get("cwd"):
                cwd = expand(raw["cwd"], values)
            if cwd:
                cwd = normalize(cwd)
            command = (
                [expand(argument, values) for argument in raw["argv"]]
                if "argv" in raw
                else expand(raw["run"], values)
            )
            if isinstance(command, list) and (
                not command[0] or any("\x00" in argument for argument in command)
            ):
                raise UsageError(f"slots.{namespace or 'prepare'} ({step_id}) 의 확장된 argv 가 잘못됨")
            steps.append(
                Step(
                    id=step_id,
                    command=command,
                    cwd=cwd,
                    group=group,
                    exclusive=bool(raw.get("exclusive")),
                    ok_exit=list(raw.get("okExit") or [0]),
                    log=values["log"],
                    out=self.log_path(name, step_id, ".out.log", namespace),
                )
            )
        steps.sort(key=lambda step: step.group)
        return steps

    def refresh_steps(self, name, keep_local=False):
        known = {step_id for step_id, _, _ in self.prepare_entries()}
        unknown = [step_id for step_id in self.refresh_ids if step_id not in known]
        if unknown:
            raise UsageError(f"slots.refresh 의 단계가 prepare 에 없음: {', '.join(unknown)}")
        if keep_local and self.vcs.has_workspaces and SYNC_STEP not in self.refresh_ids:
            raise UsageError(
                f"--keep-local 은 slots.refresh 에 {SYNC_STEP} 단계가 있어야 함 (resolve 를 그 뒤에 함)"
            )
        return self.build_steps(name, only=set(self.refresh_ids))

    def check_refresh(self, names, keep_local):
        # 진짜 {baseline} 은 메인 sync 뒤에 채워야 해서 자리표시자로 설정 오류만 먼저 잡음
        saved, self.baseline = self.baseline, "0" if self.vcs.has_workspaces else ""
        try:
            for name in names:
                self.refresh_steps(name, keep_local)
        finally:
            self.baseline = saved

    def check_format(self, name):
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise UsageError(f"슬롯 이름 형식 오류 (영숫자로 시작, 영숫자·_·- 만): {name}")
        return name

    def check_name(self, name):
        self.check_format(name)
        if name not in self.names:
            raise UsageError(
                f"slots.names 에 없는 슬롯 이름: {name} (허용: {', '.join(self.names) or '없음'})"
            )
        return name

    def path_problem(self, name):
        slot = self.slot_path(name)
        if not inside_real_root(self.root, slot):
            return f"슬롯 경로가 root({self.root}) 안이 아니거나 root 자체임: {slot}"
        if is_link(slot):
            return f"슬롯 폴더가 링크·junction 임: {slot}"
        if same_path(self.main, slot) or inside_real_root(self.main, slot) or inside_real_root(slot, self.main):
            return f"슬롯 경로가 메인 작업 공간과 겹침: {slot}"
        return ""
