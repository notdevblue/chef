"""Stable command routing for the entry point shipped with each Chef bundle."""

import argparse
import json
import os
import sys
from importlib import import_module

from chef.locations import PLUGIN_ROOT
from chef.mission.contracts import RUNTIMES

SCRIPT = PLUGIN_ROOT / "chef_cli.py"
TRANSITIONS = ("open", "resume", "close")
MISSION_STATE = ("init", "active", "status", "context", "close-stale")
STATE_GROUPS = (
    "question", "document", "unit", "ledger", "decision", "verify", "preflight",
    "reference", "inspect", "review", "review-open", "review-start", "review-scope", "review-run",
    "complete", "brief", "handoff", "schema", "transcripts",
)
GROUPS = {
    "slots": "chef.slots.commands",
    "dashboard": "chef.dashboard.server",
    "doctor": "chef.cli.doctor",
    "distill": "chef.cli.distill",
    "worker": "chef.workers.cli",
    "guard-check": "chef.cli.check_guard",
    "grok-state": "chef.runtimes.grok.state",
    "usage": "chef.mission.usage",
    "metrics": "chef.mission.observatory",
    "queue": "chef.mission.supervisor",
    "recommend": "chef.mission.routing",
}
RUNTIME_GROUPS = {"slots", "dashboard", "doctor", "distill", "guard-check"}
UNSCOPED_STATE_COMMANDS = {
    ("inspect",), ("unit", "template"), ("question", "patch"), ("question", "template"), ("document", "compare"),
}


def transition_args(argv):
    if len(argv) >= 2 and argv[0] == "mission" and argv[1] in TRANSITIONS:
        return argv[1:]
    return None


def require_runtime(parser, argv):
    # Only options before an explicit argv separator select the Chef runtime.
    options = argv[:argv.index("--")] if "--" in argv else argv
    if not any(arg in ("--runtime", "--help", "-h") or arg.startswith("--runtime=") for arg in options):
        parser.error("specify the current host with --runtime; use context to inspect its workspace")


def context(argv):
    from chef.guards import mission as guard
    from chef.infrastructure import configuration, errors

    parser = argparse.ArgumentParser(prog="chef context", allow_abbrev=False)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--project")
    parser.add_argument("--session", help="compare this session to the recorded ACTIVE owner")
    parser.add_argument("--json", action="store_true", help="JSON output (default)")
    args = parser.parse_args(argv)
    result = {"schema": 1, "bundle": str(PLUGIN_ROOT), "entrypoint": str(SCRIPT),
              "runtime": args.runtime, "cwd": os.getcwd()}
    try:
        if args.runtime == "hermes":
            lines = (PLUGIN_ROOT / "hermes/plugin.yaml").read_text(encoding="utf-8").splitlines()
            result["version"] = next(line.partition(":")[2].strip() for line in lines if line.startswith("version:"))
        else:
            manifest = PLUGIN_ROOT / f".{args.runtime}-plugin" / "plugin.json"
            result["version"] = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        located = configuration.locate(args.project, args.runtime)
        adapter = configuration.read_adapter(located.adapter_path, args.runtime)
        state_dir = guard.state_dir_of(located.project, adapter)
        active = guard.read_active(state_dir)
        owner_matches = None
        if args.session and active.id and not active.corrupt:
            owner_matches = active.session == args.session and active.runtime == args.runtime
        result.update(project=located.project, adapter=located.adapter_path, stateDir=state_dir,
                      active=active._asdict(), ownerMatches=owner_matches)
    except (errors.UsageError, guard.GuardOff, OSError, ValueError, KeyError, StopIteration) as error:
        result["error"] = str(error)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if "error" in result or result.get("active", {}).get("corrupt") else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="chef", allow_abbrev=False)
    parser.add_argument("command", choices=("context", "mission", *GROUPS, *STATE_GROUPS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parser.epilog = "For options use chef <command> --help. Put the runtime after the command: chef context --runtime codex."
    if not argv:
        parser.print_help()
        return 0
    parsed = parser.parse_args(argv)
    command, args = parsed.command, parsed.args
    if command == "context":
        return context(args)
    if command in GROUPS:
        if command in RUNTIME_GROUPS:
            require_runtime(parser, args)
        return import_module(GROUPS[command]).main(argv=args)
    from chef.mission import state
    if command != "mission":
        if (command,) not in UNSCOPED_STATE_COMMANDS and tuple(argv[:2]) not in UNSCOPED_STATE_COMMANDS:
            require_runtime(parser, args)
        return state.main([command, *args], unified=True)
    transition = transition_args(argv)
    if transition is not None:
        from chef.mission import transitions
        if transition[1:] in (["--help"], ["-h"]):
            transitions.parser().print_help()
            return 0
        return transitions.main(transition)
    if args and args[0] == "enter":
        from chef.cli import mission_entry
        require_runtime(parser, args)
        return mission_entry.main(args[1:])
    if args and args[0] in MISSION_STATE:
        require_runtime(parser, args)
        return state.main(args, unified=True)
    if args and args[0] == "set":
        require_runtime(parser, args)
        return state.main(["mission", *args], unified=True)
    mission = argparse.ArgumentParser(prog="chef mission", allow_abbrev=False)
    mission.add_argument("action", choices=("enter", *TRANSITIONS, *MISSION_STATE, "set"))
    if not args:
        mission.print_help()
        return 0
    mission.parse_args(args)
