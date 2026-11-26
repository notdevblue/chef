"""Cross-runtime worker control shared by Claude, Codex and Grok."""

import argparse
import json

from chef.workers import controller
from chef.workers.store import Store
from chef.infrastructure.storage import atomic_write_json


def summary(value, store):
    """Keep polling output small while preserving complete local evidence."""
    fields = ("key", "token", "status", "native", "actualRole", "route", "created", "cancellationRequested", "cancelNative", "instruction")
    output = {name: value[name] for name in fields if name in value}
    output["mode"] = value["request"].get("mode", "auto")
    directory = controller.attempt_directory(store, value)
    output["details"] = str(directory)
    if value.get("native") and value.get("status") in ("reserved", "running"):
        output["brief"] = value.get("brief", value["request"]["prompt"])
    result = value.get("result")
    if isinstance(result, dict):
        result_path = store.root / "details" / ("result-" + controller.digest(result) + ".json")
        if not result_path.exists():
            atomic_write_json(result_path, result, create_parents=True)
        output["details"] = str(result_path)
        compact = {name: result[name] for name in ("ok", "errorCode", "session_id", "exit_code") if name in result}
        truncated = False
        for name in ("result", "error"):
            text = result.get(name)
            if isinstance(text, str):
                compact[name] = text[:2000]
                truncated = truncated or len(text) > 2000
        for name in ("evidence", "files"):
            evidence = result.get(name)
            if isinstance(evidence, dict):
                compact[name] = dict(list(evidence.items())[:20])
                truncated = truncated or len(evidence) > 20
        compact["truncated"] = truncated
        output["result"] = compact
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="private shared worker state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--request", required=True)
    dispatch.add_argument("--mode", choices=controller.MODES,
                          help="override request, environment and project worker routing mode")
    for name in ("status", "wait", "cancel", "native-result", "recover-owner", "reconcile", "supervise"):
        command = sub.add_parser(name)
        command.add_argument("--key", required=True)
        command.add_argument("--details", action="store_true", help="include the complete stored request and result")
        if name not in ("status", "wait"):
            command.add_argument("--token", required=True)
        if name == "native-result":
            command.add_argument("--result", required=True)
        if name == "wait":
            command.add_argument("--seconds", type=int, default=30)
        if name == "recover-owner":
            command.add_argument("--runtime", choices=controller.RUNTIMES, required=True)
            command.add_argument("--session", required=True)
        if name == "reconcile":
            command.add_argument("--evidence", action="append", required=True)
    args = parser.parse_args()
    try:
        store = Store(args.home)
        if args.command == "dispatch":
            request = controller.read_json(args.request)
            if args.mode is not None:
                request["mode"] = args.mode
            value = controller.dispatch(request, store=store)
        elif args.command == "status":
            value = controller.status(args.key, store=store)
        elif args.command == "cancel":
            value = controller.cancel(args.key, args.token, store=store)
        elif args.command == "wait":
            value = controller.wait(args.key, seconds=args.seconds, store=store)
        elif args.command == "recover-owner":
            value = controller.recover_owner(args.key, args.token, args.runtime, args.session, store=store)
        elif args.command == "reconcile":
            value = controller.reconcile(args.key, args.token, args.evidence, store=store)
        elif args.command == "native-result":
            value = controller.native_result(args.key, args.token, controller.read_json(args.result), store=store)
        else:
            value = controller.supervise(args.key, args.token, store=store)
        print(json.dumps(value if getattr(args, "details", False) else summary(value, store), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
