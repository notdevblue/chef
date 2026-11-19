"""cli / slots_parser."""

import argparse

from chef.slots.contracts import RUNTIMES


def build_parser():
    parser = argparse.ArgumentParser(prog="slots.py", description="슬롯 워크스페이스 관리")
    parser.add_argument("--project", help="어댑터를 찾기 시작할 폴더 (기본: 현재 폴더)")
    parser.add_argument(
        "--runtime", choices=RUNTIMES, default=None, help="어댑터 검색. 기본은 호출 런타임, Grok은 grok"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--runtime", choices=RUNTIMES, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", parents=[common]).add_argument("names", nargs="*")

    create = sub.add_parser("create", parents=[common])
    create.add_argument("names", nargs="*")
    create.add_argument("--resume", action="store_true")
    create.add_argument("--jobs", type=int, default=2)
    create.add_argument("--dry-run", action="store_true")

    refresh = sub.add_parser("refresh", parents=[common])
    refresh.add_argument("names", nargs="*")
    refresh.add_argument("--keep-local", action="store_true")
    refresh.add_argument("--discard-local", action="store_true")
    refresh.add_argument("--main-keep-local", action="store_true")
    refresh.add_argument("--dry-run", action="store_true")

    run = sub.add_parser("run", parents=[common])
    run.add_argument("name")
    run.add_argument("task", nargs="*")
    run.add_argument("--editor", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--new-session", action="store_true")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--keep-local", action="store_true")
    run.add_argument("--discard-local", action="store_true")
    run.add_argument("--main-keep-local", action="store_true")
    run.add_argument("--dry-run", action="store_true")

    remove = sub.add_parser("remove", parents=[common])
    remove.add_argument("names", nargs="*")
    remove.add_argument("--yes", action="store_true")
    remove.add_argument("--discard-local", action="store_true")

    inject = sub.add_parser("inject", parents=[common])
    inject.add_argument("name")
    inject.add_argument("--session", default="")
    text = inject.add_mutually_exclusive_group(required=True)
    text.add_argument("--text")
    text.add_argument("--file")
    inject.add_argument("--wait", action="store_true")
    inject.add_argument("--timeout", type=int, default=1800)
    inject.add_argument("--poll", type=int, default=15)
    inject.add_argument("--dry-run", action="store_true")
    return parser
