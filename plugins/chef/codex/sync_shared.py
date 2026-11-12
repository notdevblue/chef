"""공통 스킬 정본과 런타임 참조 포인터를 검사한다."""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = (("skills/mission/references", "codex/skills/chef-mission/references"),)
SKILLS = ("mission", "slots", "distill", "adapter-init", "doctor")
RUNTIME_REFERENCES = {"codex/skills/chef-mission/references": ("codex-roles.md",)}
DUPLICATE_PARAGRAPH_MIN_CHARS = 120
REQUIRED_REFERENCES = ("adapter-schema.md", "verification-workflow.md", "usage-policy.md", "human-writing.md",
                       "question-records.md", "mission-entry.md", "confirm-template.md", "details-template.md",
                       "examples/answered.md", "examples/design-confirm.md", "examples/question.md", "examples/report.md",
                       "templates/answered.md", "templates/design-confirm.md", "templates/mission.md", "templates/report.md",
                       "templates/question.json", "templates/units.tsv", "templates/ledger.tsv", "templates/decisions.tsv")


def shared_files():
    for source_dir, target_dir in PAIRS:
        source_root = os.path.join(ROOT, *source_dir.split("/"))
        target_root = os.path.join(ROOT, *target_dir.split("/"))
        for folder, _, names in os.walk(source_root):
            for name in sorted(names):
                if name == "claude-usage.md":
                    continue
                if name.endswith(".md"):
                    source = os.path.join(folder, name)
                    relative = os.path.relpath(source, source_root)
                    target = os.path.join(target_root, relative)
                    yield source, target


def pointer_text(source, target):
    relative = os.path.relpath(source, os.path.dirname(target)).replace(os.sep, "/")
    return f"# 공통 참조\n\n실행·작성 전에 [정본]({relative}) 전체를 읽는다. 절차·양식·예시는 정본 한곳에서 관리한다.\n"


def read_text(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def differing():
    result = []
    for source, target in shared_files():
        if os.path.isfile(target):
            if read_text(target) == pointer_text(source, target):
                continue
        result.append((source, target))
    return result


def entrypoints():
    for name in SKILLS:
        source = os.path.join(ROOT, "shared", "skills", name + ".md")
        for folder, skill in (("skills", name), ("codex/skills", "chef-" + name), ("grok/skills", "chef-" + name)):
            target = os.path.join(ROOT, *folder.split("/"), skill, "SKILL.md")
            yield source, target


def obsolete_reference_files():
    expected = {os.path.normcase(os.path.abspath(target)) for _, target in shared_files()}
    for folder, names in RUNTIME_REFERENCES.items():
        for name in names:
            expected.add(os.path.normcase(os.path.abspath(os.path.join(ROOT, *folder.split("/"), name))))
    obsolete = []
    for _, target_dir in PAIRS:
        target_root = os.path.join(ROOT, *target_dir.split("/"))
        for folder, _, names in os.walk(target_root):
            for name in sorted(names):
                path = os.path.join(folder, name)
                if os.path.normcase(os.path.abspath(path)) in expected:
                    continue
                obsolete.append(path)
    return obsolete


def normalized_text(text):
    return " ".join(text.split())


def duplicated_paragraphs(source, target):
    prose = re.sub(r"```[^\n]*\n.*?```", "", source, flags=re.DOTALL)
    runtime_text = normalized_text(target)
    for paragraph in re.split(r"\n\s*\n", prose):
        paragraph = normalized_text(paragraph)
        if paragraph.startswith("#"):
            continue
        if len(paragraph) >= DUPLICATE_PARAGRAPH_MIN_CHARS and paragraph in runtime_text:
            yield paragraph


def contract_errors():
    errors = []
    for name in REQUIRED_REFERENCES:
        path = os.path.join(ROOT, "skills", "mission", "references", name)
        if os.path.isfile(path):
            continue
        errors.append("정본 없음: " + os.path.relpath(path, ROOT).replace(os.sep, "/"))
    for path in obsolete_reference_files():
        errors.append("불필요한 참조 사본: " + os.path.relpath(path, ROOT).replace(os.sep, "/"))
    documents = set()
    for source, target in entrypoints():
        for path in (source, target):
            if os.path.isfile(path):
                documents.add(path)
            else:
                errors.append("문서 없음: " + os.path.relpath(path, ROOT).replace(os.sep, "/"))
        if os.path.isfile(target):
            relative = os.path.relpath(source, os.path.dirname(target)).replace(os.sep, "/")
            if f"]({relative})" in read_text(target):
                if os.path.isfile(source) and read_text(source).strip() in read_text(target):
                    errors.append("공통 본문 중복: " + os.path.relpath(target, ROOT).replace(os.sep, "/"))
                elif os.path.isfile(source):
                    for paragraph in duplicated_paragraphs(read_text(source), read_text(target)):
                        errors.append("공통 문단 중복: " + os.path.relpath(target, ROOT).replace(os.sep, "/")
                                      + " -> " + os.path.relpath(source, ROOT).replace(os.sep, "/")
                                      + " (" + paragraph[:60] + ")")
            else:
                errors.append("공통 절차 링크 없음: " + os.path.relpath(target, ROOT).replace(os.sep, "/"))
    for source, target in shared_files():
        if os.path.isfile(target):
            documents.add(target)
        source_name = os.path.relpath(source, os.path.join(ROOT, "skills", "mission", "references")).replace(os.sep, "/")
        if source_name.startswith(("examples/", "templates/")) or source_name.endswith("-template.md"):
            continue
        documents.add(source)
    documents.add(os.path.join(ROOT, "skills", "mission", "references", "claude-usage.md"))
    for path in sorted(documents):
        if not os.path.isfile(path):
            errors.append("문서 없음: " + os.path.relpath(path, ROOT).replace(os.sep, "/"))
            continue
        for relative in re.findall(r"\]\(([^)]+)\)", read_text(path)):
            relative = relative.split("#", 1)[0]
            if relative == "" or re.match(r"[a-z]+://", relative):
                continue
            if os.path.isfile(os.path.normpath(os.path.join(os.path.dirname(path), relative))):
                continue
            errors.append("링크 대상 없음: " + os.path.relpath(path, ROOT).replace(os.sep, "/") + " -> " + relative)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Codex 공통 참조 포인터를 갱신한다")
    args = parser.parse_args(argv)
    diffs = differing()
    for source, target in diffs:
        rel = os.path.relpath(target, ROOT).replace(os.sep, "/")
        if args.write:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(pointer_text(source, target))
            print(f"포인터 갱신: {rel}")
        else:
            print(f"포인터 불일치: {rel}")
    errors = contract_errors()
    for error in errors:
        print(error)
    if errors:
        return 1
    if diffs and args.write == False:
        print("`python codex/sync_shared.py --write`로 참조 포인터를 갱신할 것")
        return 1
    print(f"공통 스킬 {len(SKILLS)}개·진입점 {len(list(entrypoints()))}개·참조 포인터 {len(list(shared_files()))}개 확인")
    return 0


if __name__ == "__main__":
    sys.exit(main())
