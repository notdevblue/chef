"""Cross-runtime worker control shared by Claude, Codex and Grok."""

import argparse
import json

from chef.cli.input_json import stdin_object
from chef.guards.adapters import GuardOff
from chef.infrastructure.errors import UsageError
from chef.mission.contracts import StateError
from chef.verification.contracts import VerificationError

from chef.workers import controller
from chef.workers.store import Store
from chef.infrastructure.storage import atomic_write_json


def summary(value, store):
    """Keep polling output small while preserving complete local evidence."""
    fields = ("key", "token", "status", "native", "actualRole", "route", "created", "cancellationRequested", "cancelNative", "instruction")
    output = {name: value[name] for name in fields if name in value}
    output["mode"] = value["request"].get("mode", "auto")
    output["allowedRuntimes"] = value["request"].get("allowedRuntimes", list(controller.RUNTIMES))
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
        if value["request"].get("reviewBinding") and result.get("ok") is True:
            compact["review"] = {"findingsCount": len(result["review"]["findings"]),
                                 "resolvedCount": len(result["review"]["resolvedIds"])}
        output["result"] = compact
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="private shared worker state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    dispatch = sub.add_parser("dispatch", allow_abbrev=False)
    dispatch.add_argument("unit", nargs="?", help="dispatch from the saved unit brief and ACTIVE mission")
    dispatch.add_argument("--request", help="explicit request JSON file, or - for standard input")
    dispatch.add_argument("--stored-brief", action="store_true", help="reuse the approved unit brief")
    dispatch.add_argument("--role")
    dispatch.add_argument("--runtime", choices=controller.RUNTIMES)
    dispatch.add_argument("--session", help="current coordinator session ID")
    dispatch.add_argument("--request-id", help="stable ID for retrying this same dispatch")
    dispatch.add_argument("--project")
    dispatch.add_argument("--file", action="append", help="select an approved implementation file")
    dispatch.add_argument("--input-path", action="append", help="additional read-only input to fingerprint")
    dispatch.add_argument("--host-role", action="append")
    dispatch.add_argument("--host-tool", action="append")
    dispatch.add_argument("--required-tool", action="append")
    dispatch.add_argument("--previous", help="prior job key for same-role follow-up work")
    dispatch.add_argument("--prompt", help="extra instructions specific to this worker")
    dispatch.add_argument("--review-kind", choices=("poc", "unit", "integration"))
    dispatch.add_argument("--receipt", help="review receipt, defaults to the latest successful verification")
    dispatch.add_argument("--mode", choices=controller.MODES,
                          help="override request, environment, project and global worker routing mode")
    for name in ("status", "wait", "cancel", "native-result", "recover-owner", "reconcile", "supervise"):
        command = sub.add_parser(name)
        command.add_argument("--key", required=True)
        command.add_argument("--details", action="store_true", help="include the complete stored request and result")
        if name not in ("status", "wait"):
            command.add_argument("--token", required=True)
        if name == "native-result":
            command.add_argument("--result", required=True, help="result JSON file, or - for standard input")
        if name == "wait":
            command.add_argument("--seconds", type=int, default=30)
        if name == "recover-owner":
            command.add_argument("--runtime", choices=controller.RUNTIMES, required=True)
            command.add_argument("--session", required=True)
        if name == "reconcile":
            command.add_argument("--evidence", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        store = Store(args.home)
        if args.command == "dispatch":
            if bool(args.unit) == bool(args.request):
                raise ValueError("supply a unit or --request, exactly one")
            if args.unit:
                from chef.workers.unit_requests import build

                request = build(args)
            else:
                if any(getattr(args, field) is not None for field in (
                    "role", "runtime", "session", "request_id", "project", "file", "input_path",
                    "host_role", "host_tool", "required_tool", "previous", "prompt", "review_kind", "receipt",
                )):
                    raise ValueError("unit dispatch options cannot be combined with --request")
                request = stdin_object() if args.request == "-" else controller.read_json(args.request)
                if args.stored_brief:
                    request["useStoredBrief"] = True
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
            result = stdin_object() if args.result == "-" else controller.read_json(args.result)
            value = controller.native_result(args.key, args.token, result, store=store)
        else:
            value = controller.supervise(args.key, args.token, store=store)
        print(json.dumps(value if getattr(args, "details", False) else summary(value, store), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, GuardOff, UsageError, StateError, VerificationError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
