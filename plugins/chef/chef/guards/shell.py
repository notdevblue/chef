"""guards / shell."""

import re

from chef.guards.contracts import (
    BACKTICK_BODY,
    BASH_ASSIGN,
    BASH_GROUP_OPENER,
    CHAIN_PREFIX,
    COMMAND_SUBSTITUTION,
    CONTINUATIONS,
    ESCAPE_CHARS,
    FIRST_TOKEN_REST,
    LITERAL_ARGUMENT,
    LITERAL_COMMAND,
    LITERAL_SEARCH_OPTIONS,
    MAX_DEPTH,
    POWERSHELL_ASSIGN,
    POWERSHELL_GROUP_OPENER,
    POWERSHELL_SUBEXPRESSION_OPENER,
    QUOTED_TEXT,
    SIMPLE_VALUE,
    VARIABLE_REFERENCE,
    WRAPPERS,
)


class Variables:
    def __init__(self, fold_case):
        self.fold_case = fold_case
        self.values = {}

    def _key(self, name):
        return name.lower() if self.fold_case else name

    def put(self, name, value):
        self.values[self._key(name)] = value

    def lookup(self, name):
        return self.values.get(self._key(name))


def join_continuations(command, tool_name="Bash"):
    return CONTINUATIONS.get(tool_name, CONTINUATIONS["Bash"]).sub(" ", command)


def is_redirect_ampersand(command, index):
    before = command[index - 1] if index > 0 else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before == ">" or after == ">"


def split_fragments(command, tool_name="Bash", quote_aware=True):
    escape = ESCAPE_CHARS.get(tool_name, "\\")
    fragments = []
    current = []
    quote = ""

    def flush():
        text = "".join(current).strip()
        current.clear()
        if text:
            fragments.append(text)

    index = 0
    while index < len(command):
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""
        if quote:
            current.append(char)
            if char == escape and quote == '"' and following:
                current.append(following)
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
        elif quote_aware and char == escape and following in ("'", '"'):
            current.append(char + following)
            index += 2
        elif quote_aware and char in "'\"":
            quote = char
            current.append(char)
            index += 1
        elif command.startswith("${", index):
            end = command.find("}", index)
            end = len(command) - 1 if end < 0 else end
            current.append(command[index : end + 1])
            index = end + 1
        elif command[index : index + 2] in ("&&", "||"):
            flush()
            index += 2
        elif char == "&" and is_redirect_ampersand(command, index):
            current.append(char)
            index += 1
        elif char in "\r\n;|&{}":
            flush()
            index += 1
        else:
            current.append(char)
            index += 1
    flush()
    if quote:
        # 닫히지 않은 따옴표(echo it's ...)가 뒤 명령을 삼키지 않게 따옴표 무시로 다시 나눔
        return split_fragments(command, tool_name, quote_aware=False)
    return fragments


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def substitute(fragment, variables):
    def replace(match):
        name = match.group(2) or match.group(3) or match.group(4)
        value = variables.lookup(name)
        return match.group(0) if value is None else value

    return VARIABLE_REFERENCE.sub(replace, fragment)


def strip_chain_prefix(fragment):
    return CHAIN_PREFIX.sub("", fragment).strip()


def peel_assignment(fragment, variables):
    while True:
        powershell = POWERSHELL_ASSIGN.match(fragment)
        match = powershell or BASH_ASSIGN.match(fragment)
        if not match:
            return fragment
        name, value = match.group(1), match.group(2).strip()
        if SIMPLE_VALUE.match(value):
            variables.put(name, unquote(value))
            return fragment
        if powershell or any(mark in value for mark in COMMAND_SUBSTITUTION):
            return strip_chain_prefix(value)
        fragment = FIRST_TOKEN_REST.match(value).group(1)


def peel_wrapper(fragment):
    for wrapper in WRAPPERS:
        match = wrapper.match(fragment)
        if match:
            return unquote(fragment[match.end() :].strip())
    return ""


def group_bodies(source, scan, opener):
    bodies = []
    skip_until = 0
    for match in opener.finditer(scan):
        if match.start() < skip_until:
            continue
        depth = 0
        end = -1
        for position in range(match.end() - 1, len(scan)):
            if scan[position] == "(":
                depth += 1
            elif scan[position] == ")":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        stop = len(scan) if end < 0 else end
        bodies.append(source[match.end() : stop])
        skip_until = stop
    return bodies


def substitution_bodies(text, tool_name):
    if tool_name == "PowerShell":
        masked = QUOTED_TEXT.sub(lambda m: "_" * len(m.group(0)), text)
        # 큰따옴표 안 $(...) 는 PowerShell 이 실행하므로 원문에서, 맨 괄호만 따옴표 가린 본문에서 찾음
        return group_bodies(text, text, POWERSHELL_SUBEXPRESSION_OPENER) + group_bodies(
            text, masked, POWERSHELL_GROUP_OPENER
        )
    return group_bodies(text, text, BASH_GROUP_OPENER) + BACKTICK_BODY.findall(text)


def expand(text, tool_name, variables, depth):
    fragments = []
    if depth > MAX_DEPTH:
        return fragments
    for raw in split_fragments(text, tool_name):
        fragment = strip_chain_prefix(substitute(raw, variables))
        fragment = strip_chain_prefix(peel_assignment(fragment, variables))
        if not fragment:
            continue
        fragments.append(fragment)
        inner = peel_wrapper(fragment)
        if inner:
            fragments.extend(expand(inner, tool_name, variables, depth + 1))
    for body in substitution_bodies(text, tool_name):
        fragments.extend(expand(body, tool_name, variables, depth + 1))
    return fragments


def command_fragments(command, tool_name="Bash"):
    variables = Variables(fold_case=tool_name == "PowerShell")
    fragments = expand(join_continuations(command, tool_name), tool_name, variables, 0)
    return list(dict.fromkeys(fragments))


def rule_exceptions(rule, problems, key="except"):
    patterns = rule.get(key, [])
    if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
        problems.append(f"guard.deny.{key} 형식 오류: 문자열 배열이어야 함. 이 규칙의 예외를 적용하지 않음")
        return []
    compiled = []
    flags = pattern_flags(rule, problems, "guard.deny")
    if flags is None:
        return []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, flags))
        except re.error as e:
            problems.append(f"guard.deny.{key} 정규식 오류: {pattern} ({e}). 이 규칙의 예외를 적용하지 않음")
            return []
    return compiled


def pattern_flags(rule, problems, label):
    sensitive = rule.get("caseSensitive", False) if isinstance(rule, dict) else False
    if type(sensitive) is not bool:
        problems.append(f"{label}.caseSensitive 는 bool 이어야 함. 이 항목 건너뜀")
        return None
    return 0 if sensitive else re.IGNORECASE


def compile_patterns(values, label, problems):
    if values is None:
        return []
    if not isinstance(values, list):
        problems.append(f"{label} 형식 오류: 배열이어야 함. 적용하지 않음")
        return []
    compiled = []
    for value in values:
        pattern = value.get("pattern") if isinstance(value, dict) else value
        if not isinstance(pattern, str):
            problems.append(f"{label} 형식 오류: 문자열 또는 {{pattern}} 이어야 함. 이 항목 건너뜀")
            continue
        flags = pattern_flags(value, problems, label)
        if flags is None:
            continue
        try:
            compiled.append(re.compile(pattern, flags))
        except re.error as e:
            problems.append(f"{label} 정규식 오류: {pattern} ({e}). 이 항목 건너뜀")
    return compiled


def allowed_fragments(adapter, fragments, problems):
    # guard.allow: 조각 전체가 맞으면 그 조각은 deny·p4Deny 를 보지 않음(전역 허용 목록). p4Client 검사는 그대로
    allows = compile_patterns(((adapter or {}).get("guard") or {}).get("allow"), "guard.allow", problems)
    if not allows:
        return fragments
    return [fragment for fragment in fragments if not any(allow.fullmatch(fragment) for allow in allows)]


def literal_data_command(command):
    match = LITERAL_COMMAND.fullmatch(command.strip(" \t"))
    if not match:
        return False
    name = match.group("name").lower().removesuffix(".exe")
    if name in ("rg", "grep"):
        arguments = [unquote(item.group(0)) for item in re.finditer(LITERAL_ARGUMENT, match.group("args"))]
        return all(
            not item.startswith("\\") and (not item.startswith("-") or item in LITERAL_SEARCH_OPTIONS)
            for item in arguments
        )
    return True
