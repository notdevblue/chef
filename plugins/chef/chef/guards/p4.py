"""guards / p4."""

import os
import re
import ntpath

from chef.guards.adapters import normalize_path
from chef.guards.contracts import (
    P4_EXECUTABLES,
    P4_READ_FLAG_COMMANDS,
    P4_TOKEN,
    P4_VALUE_OPTIONS,
    P4_WRITE_COMMANDS,
    P4C_CALL,
    P4C_OWN_VALUE_OPTIONS,
    P4C_SCRIPT,
)
from chef.guards.shell import unquote
from chef.infrastructure.paths import same_path


def p4_deny_reason(adapter, fragments, problems):
    """guard.p4Deny: p4 하위 명령 이름으로 막는 규칙. 정규식으로 p4 실행 파일·전역 옵션을 다시 쓰지 않는다.

    [{"commands": ["submit"], "when": "<나머지 인자 정규식>", "unless": "<정규식>", "reason": "..."}]
    when 이 있으면 맞을 때만, unless 가 맞으면 막지 않음. 나머지 인자는 따옴표를 벗겨 공백으로 이은 문자열.
    """
    rules = ((adapter or {}).get("guard") or {}).get("p4Deny")
    if not rules:
        return ""
    if not isinstance(rules, list):
        problems.append("guard.p4Deny 형식 오류: 배열이어야 함. 적용하지 않음")
        return ""
    parsed = [invocation for invocation in map(p4_invocation, fragments) if invocation]
    if not parsed:
        return ""
    for rule in rules:
        commands = rule.get("commands") if isinstance(rule, dict) else None
        if isinstance(commands, str):
            commands = [commands]
        if (
            not isinstance(commands, list)
            or not commands
            or not all(isinstance(item, str) for item in commands)
        ):
            problems.append("guard.p4Deny.commands 형식 오류: 문자열 배열이어야 함. 이 규칙 건너뜀")
            continue
        names = {item.lower() for item in commands}
        conditions = {}
        try:
            for key in ("when", "unless"):
                value = rule.get(key)
                conditions[key] = (
                    re.compile(value, re.IGNORECASE) if isinstance(value, str) and value else None
                )
        except re.error as e:
            problems.append(f"guard.p4Deny 정규식 오류: {e}. 이 규칙 건너뜀")
            continue
        for _, command, rest in parsed:
            if command not in names:
                continue
            text = " ".join(rest)
            if conditions["when"] and not conditions["when"].search(text):
                continue
            if conditions["unless"] and conditions["unless"].search(text):
                continue
            return str(rule.get("reason") or f"p4 {command} 금지")
    return ""


def expected_p4_client(project, adapter):
    # 이 폴더가 쓸 작업 공간: 슬롯 폴더면 slots.client 틀, 메인 폴더면 slots.mainClient, 아니면 .p4config 의 P4CLIENT
    if not project:
        return ""
    slots = (adapter or {}).get("slots") or {}
    root, template = slots.get("root"), slots.get("client")
    parent, name = os.path.split(os.path.normpath(project))
    if isinstance(root, str) and root and isinstance(template, str) and "{name}" in template and name:
        if normalize_path(parent) == normalize_path(root):
            return template.replace("{name}", name)
    main = ((adapter or {}).get("vars") or {}).get("main")
    main_client = slots.get("mainClient")
    if isinstance(main, str) and main and isinstance(main_client, str) and main_client:
        if normalize_path(project) == normalize_path(main):
            return main_client
    try:
        with open(os.path.join(project, ".p4config"), encoding="utf-8-sig") as f:
            match = re.search(r"^P4CLIENT=(.*)$", f.read(), re.M)
    except OSError:
        return ""
    return match.group(1).strip() if match else ""


def p4_invocation(fragment):
    """p4 호출이면 (전역 -c 값 또는 None, 하위 명령, 나머지 인자), 아니면 None."""
    tokens = list(fragment) if isinstance(fragment, (list, tuple)) else [unquote(token) for token in P4_TOKEN.findall(fragment)]
    if tokens and tokens[0] == "&":  # PowerShell 호출 연산자
        tokens = tokens[1:]
    if not tokens or re.split(r"[\\/]", tokens[0])[-1].lower() not in P4_EXECUTABLES:
        return None
    client, index = None, 1
    while index < len(tokens) and tokens[index].startswith("-") and len(tokens[index]) > 1:
        flag = tokens[index][:2]
        if flag in P4_VALUE_OPTIONS:
            value = tokens[index][2:]
            if not value:
                index += 1
                value = tokens[index] if index < len(tokens) else ""
            if flag == "-c":
                client = value
        index += 1
    command = tokens[index].lower() if index < len(tokens) else ""
    return client, command, tokens[index + 1 :]


def p4c_as_p4(fragment, project="", adapter=None):
    """p4c.py 래퍼 호출을 같은 효과의 `p4 -c <작업 공간> ...` 조각으로 바꿈(deny 규칙이 래퍼로 우회되지 않게)."""
    structured = isinstance(fragment, (list, tuple))
    if structured:
        tokens = list(fragment)
        executable = os.path.basename(tokens.pop(0)) if tokens else ""
        if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", executable, re.IGNORECASE):
            while tokens and tokens[0].startswith("-"):
                flag = tokens.pop(0)
                if flag in ("-c", "-m"):
                    return ""
                if flag in ("-X", "-W", "--check-hash-based-pycs") and tokens:
                    tokens.pop(0)
            executable = os.path.basename(tokens.pop(0)) if tokens else ""
        if executable.lower() != "p4c.py":
            return ""
    else:
        match = P4C_CALL.match(fragment)
        if not match:
            return ""
        tokens = P4_TOKEN.findall(match.group(1))
    client = ""
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if flag == "--":
            break
        if flag in P4C_OWN_VALUE_OPTIONS and tokens:
            value = tokens.pop(0)
            if not structured:
                value = unquote(value)
            if flag == "--client":
                client = value
    client = client or expected_p4_client(project, adapter) or "p4c-client"
    result = ["p4", "-c", client, *tokens]
    return result if structured else " ".join(result)


def p4_policy(adapter):
    policy = ((adapter or {}).get("guard") or {}).get("p4Client")
    if not isinstance(policy, dict) or not policy.get("require"):
        return None
    commands = policy.get("commands")
    if isinstance(commands, list) and all(isinstance(item, str) for item in commands):
        return frozenset(item.lower() for item in commands)
    return P4_WRITE_COMMANDS


def p4c_project_reason(fragment, project="", adapter=None, cwd=None):
    """The wrapper's explicit project must not redirect protected operations."""
    commands = p4_policy(adapter)
    wrapped = p4c_as_p4(fragment, project, adapter)
    parsed = p4_invocation(wrapped) if wrapped else None
    if commands is None or not parsed:
        return ""
    _, command, rest = parsed
    if command not in commands or (command in P4_READ_FLAG_COMMANDS and "-o" in rest):
        return ""
    structured = isinstance(fragment, (list, tuple))
    if structured:
        tokens = list(fragment)
        script = next((index for index, value in enumerate(tokens) if os.path.basename(value).lower() == "p4c.py"), -1)
        tokens = tokens[script + 1:]
    else:
        match = P4C_CALL.match(fragment)
        tokens = P4_TOKEN.findall(match.group(1))
    explicit = ""
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if flag == "--":
            break
        if flag in P4C_OWN_VALUE_OPTIONS and tokens:
            value = tokens.pop(0)
            if flag == "--project":
                explicit = value if structured else unquote(value)
    if explicit:
        if ntpath.splitdrive(project)[0]:
            target = ntpath.join(project, explicit)
            equivalent = same_path(project, target) if os.name == "nt" else ntpath.normcase(ntpath.normpath(project)) == ntpath.normcase(ntpath.normpath(target))
        else:
            equivalent = bool(project) and same_path(project, os.path.join(project, explicit))
        if not equivalent:
            return "p4c --project 가 허용된 작업 공간과 다름. 현재 프로젝트의 절대 경로를 사용할 것"
    elif cwd and not same_path(project, cwd):
        return "p4c 실행 cwd가 프로젝트 루트와 다름. --project 에 허용된 프로젝트의 절대 경로를 지정할 것"
    return ""


def p4_client_reason(adapter, fragments, project=""):
    commands = p4_policy(adapter)
    if commands is None:
        return ""
    expected = expected_p4_client(project, adapter)
    for fragment in fragments:
        parsed = p4_invocation(fragment)
        if not parsed:
            continue
        client, command, rest = parsed
        if command not in commands or (command in P4_READ_FLAG_COMMANDS and "-o" in rest):
            continue
        if not client:
            return (
                f"p4 {command} 는 전역 -c <작업 공간> 필수(P4CLIENT·PWD 함정). "
                f'래퍼로: python "{P4C_SCRIPT}" {command} ... (작업 공간 {expected or "자동"} 고정)'
            )
        unresolved = not isinstance(fragment, (list, tuple)) and ("$" in client or "{" in client)
        if expected and not unresolved and client.casefold() != expected.casefold():
            return f"p4 -c {client} 가 이 폴더의 작업 공간({expected})과 다름 — 다른 슬롯·메인 작업 공간에 쓰지 않음"
    return ""
