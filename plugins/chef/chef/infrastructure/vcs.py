"""infrastructure / vcs."""

import os
import re
import subprocess

from chef.infrastructure.errors import UsageError
from chef.infrastructure.paths import same_path
from chef.infrastructure.process import child_env, decode, resolve_p4

BASELINE_LINE = re.compile(r"Change (\d+) on ")


CLIENT_NAME_LINE = re.compile(r"^Client name:\s*(.+)$", re.M)


CHURN_TOKEN = re.compile(r"(\.\.\.|\*)")


CHURN_WILDCARDS = {"...": ".*", "*": "[^/]*"}


DEPOT_LINE_PATH = re.compile(r"^(//.+?)(?:#\d+)? - ")


WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


SLOT_DESCRIPTION = "AI mission slot {name} (chef)"


SLOT_MARKER = "AI mission slot"


MAIN_KEEP_DESCRIPTION = "han main keep-local"


NO_RECONCILE = "no file(s) to reconcile"


class Completed:
    def __init__(self, code, out, err):
        self.code = code
        self.out = out
        self.err = err

    @property
    def lines(self):
        return [line for line in self.out.splitlines() if line.strip()]


def spec_pattern(key):
    return re.compile(rf"^{key}:(?P<v>[^\n]*(?:\n[ \t]+[^\n]*)*)", re.M)


def get_field(spec, key):
    match = spec_pattern(key).search(spec)
    return match.group("v").strip() if match else ""


def set_field(spec, key, value, block=False):
    text = f"{key}:\n\t{value}" if block else f"{key}:\t{value}"
    pattern = spec_pattern(key)
    if pattern.search(spec):
        return pattern.sub(lambda _: text, spec, count=1)
    return spec.rstrip("\n") + "\n\n" + text + "\n"


def p4config_client(slot):
    try:
        with open(os.path.join(slot, ".p4config"), encoding="utf-8-sig") as f:
            match = re.search(r"^P4CLIENT=(.*)$", f.read(), re.M)
    except OSError:
        return ""
    return match.group(1).strip() if match else ""


class NoVcs:
    name = "none"
    has_workspaces = False
    executable = None

    def client_exists(self, client):
        return False

    def identity_problem(self, name, client, slot):
        return ""

    def create_workspace(self, name, client, slot):
        pass

    def write_config(self, client, slot):
        pass

    def opened(self, client):
        return []

    def shelved(self, client):
        return []

    def pending(self, client):
        return []

    def local_changes(self, client, edits_only=False):
        return [], 0

    def is_churn(self, line):
        return False

    def restore_churn(self, client):
        pass

    def describe_churn(self, client):
        return []

    def resolve_conflicts(self, client):
        return []

    def describe_keep_local(self, client, description):
        return []

    def describe_resolve(self, client):
        return []

    def workspace_name_in(self, slot):
        return ""

    def delete_workspace(self, client):
        pass

    def describe_create(self, name, client, slot):
        return []

    def baseline(self):
        raise UsageError("{baseline} 은 slots.vcs 가 있어야 씀")


class Perforce:
    name = "perforce"
    has_workspaces = True

    def __init__(self, argv, main_client, stream, main, churn=()):
        self.argv = argv
        self.main_client = main_client
        self.stream = stream
        self.main = main
        self.churn = list(churn)
        self.churn_patterns = [churn_pattern(stream, entry) for entry in self.churn]

    @property
    def executable(self):
        return self.argv[0]

    def call(self, client, *args, stdin=""):
        command = [*self.argv, "-c", client, *args]
        try:
            done = subprocess.run(
                command,
                cwd=self.main,
                env=child_env(self.main, self.executable),
                input=stdin.encode("utf-8"),
                capture_output=True,
            )
        except OSError as e:
            return Completed(127, "", f"{command[0]} 실행 실패: {e}")
        return Completed(done.returncode, decode(done.stdout), decode(done.stderr))

    def checked(self, client, *args, stdin=""):
        done = self.call(client, *args, stdin=stdin)
        if done.code != 0:
            raise RuntimeError(
                f"p4 -c {client} {' '.join(args)} 실패: {done.err.strip() or done.out.strip()}"
            )
        return done

    def client_exists(self, client):
        return bool(self.checked(self.main_client, "clients", "-e", client).lines)

    def identity_problem(self, name, client, slot):
        if client.casefold() == self.main_client.casefold():
            return f"슬롯 작업 공간이 메인 작업 공간({self.main_client})과 같음"
        if self.client_exists(client):
            spec = self.checked(self.main_client, "client", "-o", client).out
            root = get_field(spec, "Root")
            if not same_path(root, slot):
                return f"작업 공간 {client} 의 Root({root or '없음'}) 가 슬롯 경로({slot})와 다름"
            if SLOT_MARKER not in get_field(spec, "Description"):
                return f"작업 공간 {client} 의 Description 에 슬롯 표식({SLOT_MARKER}) 없음 — 슬롯용으로 만든 작업 공간이 아님"
        if os.path.isdir(slot):
            configured = p4config_client(slot)
            if configured.casefold() != client.casefold():
                return f"{os.path.join(slot, '.p4config')} 의 P4CLIENT({configured or '없음'}) 가 {client} 와 다름"
        return ""

    def create_workspace(self, name, client, slot):
        spec = self.checked(self.main_client, "client", "-o", "-S", self.stream, client).out
        main_spec = self.checked(self.main_client, "client", "-o", self.main_client).out
        for key in ("Options", "LineEnd", "SubmitOptions"):
            value = get_field(main_spec, key)
            if value:
                spec = set_field(spec, key, value)
        spec = set_field(spec, "Root", slot)
        spec = set_field(spec, "Description", SLOT_DESCRIPTION.format(name=name), block=True)
        self.checked(client, "client", "-i", stdin=spec)

    def write_config(self, client, slot):
        source = os.path.join(self.main, ".p4config")
        with open(source, encoding="utf-8", newline="") as f:
            text = f.read()
        line = f"P4CLIENT={client}"
        if re.search(r"^P4CLIENT=[^\r\n]*", text, re.M):
            text = re.sub(r"^P4CLIENT=[^\r\n]*", lambda _: line, text, count=1, flags=re.M)
        else:
            text = text.rstrip("\r\n") + "\n" + line + "\n"
        with open(os.path.join(slot, ".p4config"), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def listing(self, client, *args):
        return self.checked(client, *args).lines

    def opened(self, client):
        return [line for line in self.checked(client, "opened").out.splitlines() if line.startswith("//")]

    def shelved(self, client):
        return self.listing(client, "changes", "-s", "shelved", "-c", client)

    def pending(self, client):
        return self.listing(client, "changes", "-s", "pending", "-c", client)

    def local_changes(self, client, edits_only=False):
        flags = ["-n", "-e"] if edits_only else ["-n"]
        out = self.checked(client, "reconcile", *flags, f"//{client}/...").out
        lines = [line for line in out.splitlines() if line.startswith("//")]
        kept = [line for line in lines if not self.is_churn(line)]
        return kept, len(lines) - len(kept)

    def is_churn(self, line):
        match = DEPOT_LINE_PATH.match(line)
        return bool(match) and any(pattern.fullmatch(match.group(1)) for pattern in self.churn_patterns)

    def restore_churn(self, client):
        for entry in self.churn:
            self.checked(client, "sync", "-f", f"//{client}/{entry}#have")

    def describe_churn(self, client):
        return [f"p4 -c {client} sync -f //{client}/{entry}#have" for entry in self.churn]

    def keep_changelist(self, client, description):
        out = self.checked(client, "changes", "-s", "pending", "-c", client, "-l").out
        found = re.search(rf"^Change (\d+) on [^\n]*\n\n\t{re.escape(description)}[ \t]*$", out, re.M)
        if found:
            return found.group(1)
        spec = f"Change:\tnew\n\nClient:\t{client}\n\nDescription:\n\t{description}\n"
        created = re.search(r"Change (\d+) created", self.checked(client, "change", "-i", stdin=spec).out)
        if not created:
            raise RuntimeError(f"보관 CL({description}) 을 만들지 못함")
        return created.group(1)

    def keep_local(self, client, description, changes):
        matches = [DEPOT_LINE_PATH.match(line) for line in changes]
        if not all(matches):
            raise RuntimeError(f"로컬 변경의 파일 경로를 읽지 못해 보관 못 함: {changes[0]}")
        changelist = self.keep_changelist(client, description)
        files = "\n".join(match.group(1) for match in matches)
        self.checked(client, "-x", "-", "reconcile", "-e", "-c", changelist, stdin=files)
        return changelist, len(matches)

    def resolve_conflicts(self, client):
        self.checked(client, "resolve", "-am")
        return [
            line for line in self.checked(client, "resolve", "-n").out.splitlines() if line.startswith("//")
        ]

    def describe_keep_local(self, client, description):
        return [
            f"p4 -c {client} change -i  ({description}, 이미 있으면 재사용)",
            f"p4 -c {client} -x - reconcile -e -c <보관 CL>  (reconcile -n -e 로 찾은 열리지 않은 변경 파일)",
        ]

    def describe_resolve(self, client):
        return [f"p4 -c {client} resolve -am", f"p4 -c {client} resolve -n  (남은 충돌이 있으면 멈춤)"]

    def workspace_name_in(self, slot):
        try:
            done = subprocess.run([*self.argv, "info"], cwd=slot,
                                  env=child_env(slot, self.executable), capture_output=True)
        except OSError:
            return ""
        match = CLIENT_NAME_LINE.search(decode(done.stdout))
        return match.group(1).strip() if done.returncode == 0 and match else ""

    def delete_workspace(self, client):
        self.checked(client, "client", "-d", client)

    def latest_change(self, spec, label, flags=()):
        out = self.checked(self.main_client, "changes", "-m1", *flags, spec).out
        match = BASELINE_LINE.match(out.strip())
        if not match:
            raise RuntimeError(f"{label} 에서 기준 CL 을 못 읽음: {out.strip() or '출력 없음'}")
        return match.group(1)

    def baseline(self):
        return self.latest_change(f"//{self.main_client}/...#have", f"{self.main_client} 의 have")

    def head(self):
        # 기본값은 pending·shelved CL 도 훑어 submit 안 된 번호가 나옴
        return self.latest_change(
            f"{self.stream.rstrip('/')}/...", f"{self.stream} 의 head", ("-s", "submitted")
        )

    def sync_preview(self, head):
        out = self.checked(self.main_client, "sync", "-n", f"//{self.main_client}/...@{head}").out
        return [line for line in out.splitlines() if line.startswith("//")]

    def unopened_edits(self, pending):
        opened = {
            match.group(1).casefold()
            for match in map(DEPOT_LINE_PATH.match, self.opened(self.main_client))
            if match
        }
        paths = []
        for line in pending:
            match = DEPOT_LINE_PATH.match(line)
            if match and match.group(1).casefold() not in opened and not self.is_churn(line):
                paths.append(match.group(1))
        if not paths:
            return []
        done = self.call(self.main_client, "-x", "-", "reconcile", "-n", "-e", stdin="\n".join(paths))
        # 바뀐 파일이 없을 때 p4 가 경고로 0 아닌 종료를 내는 경우가 있음
        if done.code != 0 and NO_RECONCILE not in (done.out + done.err).lower():
            raise RuntimeError(
                f"p4 -c {self.main_client} -x - reconcile -n -e 실패: {done.err.strip() or done.out.strip()}"
            )
        return [line for line in done.out.splitlines() if line.startswith("//")]

    def sync_main(self, head):
        self.checked(
            self.main_client, "sync", "-q", "--parallel=threads=8", f"//{self.main_client}/...@{head}"
        )

    def describe_main_sync(self, head, keep_local):
        keep = self.describe_keep_local(self.main_client, MAIN_KEEP_DESCRIPTION) if keep_local else []
        sync = f"p4 -c {self.main_client} sync -q --parallel=threads=8 //{self.main_client}/...@{head}"
        return [*keep, sync, *self.describe_resolve(self.main_client)]

    def describe_create(self, name, client, slot):
        return [
            f"p4 -c {self.main_client} clients -e {client}",
            f"p4 -c {self.main_client} client -o -S {self.stream} {client}"
            f"  (Root={slot}, Options·LineEnd·SubmitOptions는 {self.main_client}에서 복사)",
            f"p4 -c {client} client -i",
            f"{os.path.join(slot, '.p4config')} 작성 (P4CLIENT={client})",
        ]


def churn_pattern(stream, entry):
    text = f"{stream.rstrip('/')}/{entry}"
    body = "".join(CHURN_WILDCARDS.get(part) or re.escape(part) for part in CHURN_TOKEN.split(text))
    return re.compile(body, re.IGNORECASE)


def churn_entries(slots):
    entries = slots.get("churn")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise UsageError(f"slots.churn 은 문자열 배열이어야 함: {entries!r}")
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise UsageError(f"slots.churn 항목은 비어 있지 않은 문자열이어야 함: {entry!r}")
        if entry.startswith(("/", "\\")) or WINDOWS_DRIVE.match(entry):
            raise UsageError(f"slots.churn 항목은 스트림 루트 기준 상대 경로여야 함: {entry}")
        if "\\" in entry:
            raise UsageError(f"slots.churn 항목은 / 로 구분해야 함: {entry}")
        if ".." in entry.split("/"):
            raise UsageError(f"slots.churn 항목에 .. 를 쓸 수 없음: {entry}")
    return list(dict.fromkeys(entries))


def build_vcs(slots, main):
    kind = slots.get("vcs") or "none"
    if kind == "none":
        return NoVcs()
    if kind != "perforce":
        raise UsageError(f"slots.vcs 는 perforce 또는 none: {kind}")
    for key in ("stream", "mainClient"):
        if not slots.get(key):
            raise UsageError(f"slots.vcs 가 perforce 면 slots.{key} 필요 (local.json 확인)")
    configured = slots.get("p4") or [None]
    executable, *arguments = configured if isinstance(configured, list) else [configured]
    return Perforce(
        [resolve_p4(executable, cwd=main), *arguments],
        slots["mainClient"],
        slots["stream"],
        main,
        churn_entries(slots),
    )
