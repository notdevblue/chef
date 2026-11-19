#!/usr/bin/env python3
"""Synthetic experiment coordinator. Artifact evaluation never grants mission approval."""

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from chef.locations import PLUGIN_ROOT
from chef.mission import state as mission_state
from chef.slots import commands as slots
from chef.verification import runner as verification

SCHEMA = 1
EVALUATOR = "interval-union-v1"
SPEC = """Implement solution.py with solve(intervals).
Input is a list of closed integer intervals [start, end], with start <= end.
Return the sorted union as a list of disjoint [start, end] lists. Merge intervals
which overlap OR touch at an endpoint; do not merge separated integer neighbors.
Do not mutate the input. Empty input returns []. Negative values and duplicates
are valid. Only the Python standard library is available. No files or network
are needed. Training examples are public.json. Hidden cases are unavailable.
"""
RUNNER = """import json, resource, runpy, sys
resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
solve = runpy.run_path('/candidate/solution.py')['solve']
cases = json.load(sys.stdin)
result = []
for value in cases:
    before = json.loads(json.dumps(value))
    try:
        answer = solve(value)
        result.append({'answer': answer, 'unchanged': value == before})
    except Exception:
        result.append({'error': 'candidate_exception'})
json.dump(result, sys.stdout, allow_nan=False)
"""


class LabError(ValueError):
    pass


def hashed(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    verification.write_json(path, value)


def config_check(config):
    if not isinstance(config, dict) or type(config.get("schema")) is not int or config["schema"] != SCHEMA:
        raise LabError("experiment schema must be 1")
    for field, lower, upper in (
        ("generations", 1, 10),
        ("candidates", 4, 20),
        ("maxEvaluations", 1, 400),
        ("maxSeconds", 1, 86400),
        ("maxConcurrent", 1, 20),
        ("seed", 0, 2**32 - 1),
    ):
        if type(config.get(field)) is not int or not lower <= config[field] <= upper:
            raise LabError(f"invalid {field}")
    if config["maxEvaluations"] < config["generations"] * config["candidates"] * 2:
        raise LabError("evaluation budget must cover development and holdout batches")
    if config.get("costPolicy") != "unknown-no-paid-launches":
        raise LabError("this artifact runner does not launch paid agents or enforce provider billing")
    strategies = config.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != config["candidates"]:
        raise LabError("one explicitly named strategy per candidate required")
    definitions = {}
    for item in strategies:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "kind", "runtime", "model", "effort"}
            or any(not isinstance(v, str) or not v for v in item.values())
            or not slots.NAME_PATTERN.fullmatch(item["name"])
            or item["kind"] not in ("single", "multi", "fixture")
        ):
            raise LabError("invalid strategy metadata")
        if item["name"] in definitions and definitions[item["name"]] != item:
            raise LabError("replicated strategy must use identical settings")
        definitions[item["name"]] = item
    return config


def oracle(intervals):
    # Independent endpoint sweep, rather than the candidate's usual merge loop.
    events = {}
    for start, end in intervals:
        events.setdefault(start, [0, 0])[0] += 1
        events.setdefault(end, [0, 0])[1] += 1
    depth, result, opened = 0, [], None
    for point, (starts, ends) in sorted(events.items()):
        if depth == 0 and starts:
            opened = point
        depth += starts - ends
        if depth == 0 and opened is not None:
            result.append([opened, point])
            opened = None
    return result


def cases(seed, count):
    rng = random.Random(seed)
    result = [
        [],
        [[1, 1]],
        [[0, 1], [1, 2]],
        [[0, 0], [1, 1]],
        [[-4, -2], [-3, 0]],
        [[3, 9], [3, 9], [-20, -19]],
    ]
    for _ in range(count):
        result.append(
            [sorted([rng.randint(-1000, 1000), rng.randint(-1000, 1000)]) for _ in range(rng.randrange(30))]
        )
    return result


def slot_names(config, generation):
    return [f"g{generation:02d}-c{index:02d}" for index in range(config["candidates"])]


def prepare(root, config):
    config_check(config)
    root = Path(root).absolute()
    if root.exists():
        raise LabError("new experiment root must not exist; use status/resume")
    root.mkdir(parents=True)
    (root / "main").mkdir()
    (root / "private").mkdir(mode=0o700)
    (root / "snapshots").mkdir()
    (root / "results").mkdir()
    save(root / "config.json", config)
    # This seed is saved only outside candidate workspaces. Publish after final evaluation.
    seed = int.from_bytes(os.urandom(16), "big")
    hidden = cases(seed, 100)
    save(root / "private" / "holdout.json", {"seed": seed, "cases": hidden})
    state = {
        "schema": SCHEMA,
        "configHash": hashed(config),
        "evaluator": EVALUATOR,
        "holdoutHash": hashed(hidden),
        "generation": 1,
        "evaluations": 0,
        "elapsedSeconds": 0.0,
        "stopped": False,
        "phase": "prepared",
        "selections": [],
        "results": [],
        "pending": None,
    }
    save(root / "state.json", state)
    prepare_generation(root, config, 1)
    return state


def prepare_generation(root, config, generation):
    root = Path(root)
    names = slot_names(config, generation)
    adapter = {
        "schema": 1,
        "project": "Synthetic Interval Lab",
        "paths": {"stateDir": "state"},
        "slots": {
            "enabled": True,
            "vcs": "none",
            "root": str(root / "slots"),
            "client": "lab-{name}",
            "names": names,
        },
    }
    located = slots.mission_guard.Located(str(root / "main"), str(root / "adapter.json"))
    save(root / "adapter.json", adapter)
    configuration = slots.Config(located, adapter, "codex")
    runner = slots.Runner(configuration)
    for index, name in enumerate(names):
        outcome = slots.create_one(runner, name, [], False)
        if outcome.failed:
            raise LabError(f"slot preparation failed: {outcome.detail}")
        folder = root / "slots" / name
        (folder / "TASK.md").write_text(SPEC, encoding="utf-8")
        public = cases(config["seed"] + generation, 20)
        save(folder / "public.json", [{"input": value, "expected": oracle(value)} for value in public])
        save(folder / "strategy.json", config["strategies"][index])
        if generation > 1:
            # Selection is based solely on development scores, never holdout scores.
            state = read(root / "state.json")
            selected = state["selections"][-1]
            shutil.copyfile(root / "snapshots" / f"{selected}.py", folder / "parent.py")


def load(root):
    root = Path(root)
    config = config_check(read(root / "config.json"))
    state = read(root / "state.json")
    if (
        not isinstance(state, dict)
        or type(state.get("schema")) is not int
        or state["schema"] != SCHEMA
        or state.get("configHash") != hashed(config)
        or state.get("evaluator") != EVALUATOR
        or type(state.get("generation")) is not int
        or not 1 <= state["generation"] <= config["generations"]
        or type(state.get("evaluations")) is not int
        or state["evaluations"] < 0
        or state["evaluations"] > config["maxEvaluations"]
        or not isinstance(state.get("results"), list)
        or not isinstance(state.get("selections"), list)
        or type(state.get("stopped")) is not bool
        or state.get("phase") not in ("prepared", "holdout", "complete")
        or type(state.get("elapsedSeconds")) not in (int, float)
        or not __import__("math").isfinite(state["elapsedSeconds"])
        or state["elapsedSeconds"] < 0
    ):
        raise LabError("corrupt experiment state/configuration")
    pending = state.get("pending")
    if pending is not None and (
        not isinstance(pending, dict)
        or set(pending) != {"id", "split", "startedAt"}
        or not isinstance(pending["id"], str)
        or pending["split"] not in ("development", "holdout")
        or pending["id"]
        not in [
            name
            for generation in range(1, state["generation"] + 1)
            for name in slot_names(config, generation)
        ]
        or state["evaluations"] < 1
        or type(pending["startedAt"]) not in (int, float)
        or not __import__("math").isfinite(pending["startedAt"])
    ):
        raise LabError("corrupt pending reservation")
    seen = set()
    for row in state["results"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row["id"] in seen
            or type(row.get("generation")) is not int
            or not 1 <= row["generation"] <= state["generation"]
            or row["id"] not in slot_names(config, row["generation"])
        ):
            raise LabError("corrupt candidate result")
        seen.add(row["id"])
        snapshot = Path(root) / "snapshots" / f"{row['id']}.py"
        if (
            not snapshot.is_file()
            or snapshot.is_symlink()
            or hashed(snapshot.read_bytes()) != row.get("codeHash")
        ):
            raise LabError("frozen candidate integrity mismatch")
        validate_score(row)
        if "holdout" in row:
            validate_score(row["holdout"])
    if (
        len(state["selections"]) > config["generations"]
        or any(not isinstance(v, str) or v not in seen for v in state["selections"])
        or len(set(state["selections"])) != len(state["selections"])
    ):
        raise LabError("corrupt selection history")
    if state["phase"] in ("holdout", "complete"):
        expected = config["generations"] * config["candidates"]
        if (
            state["generation"] != config["generations"]
            or len(state["results"]) != expected
            or len(state["selections"]) != config["generations"]
            or state["evaluations"] < expected
            or state["phase"] == "complete"
            and any("holdout" not in row for row in state["results"])
        ):
            raise LabError("incomplete evaluation history for phase")
    hidden = read(root / "private" / "holdout.json")
    if hashed(hidden["cases"]) != state.get("holdoutHash"):
        raise LabError("holdout integrity mismatch")
    return config, state, hidden


def isolated_score(code, inputs, timeout=5):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        raise LabError("arbitrary candidate execution requires Linux bubblewrap; no unsafe fallback")
    with tempfile.TemporaryDirectory(prefix="chef-evaluator-") as directory:
        folder = Path(directory)
        (folder / "runner.py").write_text(RUNNER, encoding="utf-8")
        command = [
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for library in ("/lib", "/lib64"):
            if Path(library).exists():
                command += ["--ro-bind", library, library]
        command += [
            "--dir",
            "/candidate",
            "--ro-bind",
            str(Path(code).resolve()),
            "/candidate/solution.py",
            "--ro-bind",
            str(folder / "runner.py"),
            "/runner.py",
            "--chdir",
            "/candidate",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "/usr/bin/python3",
            "-I",
            "/runner.py",
        ]
        started = time.monotonic()
        with (folder / "output").open("w+b") as output, (folder / "errors").open("w+b") as errors:
            try:
                process = subprocess.run(
                    command,
                    input=json.dumps(inputs).encode(),
                    stdout=output,
                    stderr=errors,
                    timeout=timeout,
                    check=False,
                )
                output.seek(0)
                raw = output.read(1048577)
            except subprocess.TimeoutExpired:
                return {
                    "passed": 0,
                    "total": len(inputs),
                    "seconds": time.monotonic() - started,
                    "status": "timeout",
                }
        if process.returncode:
            return {
                "passed": 0,
                "total": len(inputs),
                "seconds": time.monotonic() - started,
                "status": "execution_failed",
            }
        try:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != len(inputs):
                raise ValueError("shape")
            passed = sum(
                isinstance(value, dict)
                and value.get("unchanged") is True
                and valid_answer(value.get("answer"))
                and value["answer"] == oracle(case)
                for value, case in zip(values, inputs)
            )
        except (ValueError, TypeError):
            passed = 0
        return {
            "passed": passed,
            "total": len(inputs),
            "seconds": time.monotonic() - started,
            "status": "scored",
        }


def valid_answer(value):
    return isinstance(value, list) and all(
        isinstance(interval, list) and len(interval) == 2 and all(type(point) is int for point in interval)
        for interval in value
    )


def evaluate(root, score=isolated_score):
    root = Path(root).absolute()
    with slots.FileLock(str(root / "lab.lock"), "lab"):
        config, state, hidden = load(root)
        if state["stopped"]:
            raise LabError("experiment stopped")
        if state["phase"] == "complete":
            return publish(root, config, state, hidden)
        if state["pending"] is not None:
            raise LabError("interrupted evaluation reserved; recover explicitly before resuming")
        if state["elapsedSeconds"] >= config["maxSeconds"]:
            raise LabError("experiment time budget exhausted")
        if state["phase"] == "holdout":
            return finish_holdout(root, config, state, hidden, score)
        generation = state["generation"]
        names = slot_names(config, generation)
        # Freeze every candidate before evaluating any candidate. Holdout stays sealed.
        for name in names:
            source = root / "slots" / name / "solution.py"
            snapshot = root / "snapshots" / f"{name}.py"
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise LabError(f"missing, symlinked or oversized candidate: {name}")
            content = source.read_bytes()
            if snapshot.exists() and snapshot.read_bytes() != content:
                raise LabError("frozen candidate changed")
            snapshot.write_bytes(content)
        inputs = cases(config["seed"] + generation, 20)
        for index, name in enumerate(names):
            if any(row["id"] == name for row in state["results"]):
                continue
            row = measure(root, config, state, name, inputs, "development", score)
            row.update(
                id=name,
                generation=generation,
                strategy=config["strategies"][index],
                codeHash=hashed((root / "snapshots" / f"{name}.py").read_bytes()),
            )
            state["results"].append(row)
            save(root / "state.json", state)
        current = [row for row in state["results"] if row["generation"] == generation]
        selected = max(current, key=lambda row: (row["passed"] / row["total"], -row["seconds"], row["id"]))
        state["selections"].append(selected["id"])
        if generation < config["generations"]:
            state["generation"] += 1
            state["phase"] = "prepared"
            save(root / "state.json", state)
            prepare_generation(root, config, state["generation"])
            return report(root, config, state)
        # Holdout is evaluated only after every generation's selection is frozen.
        state["phase"] = "holdout"
        save(root / "state.json", state)
        return finish_holdout(root, config, state, hidden, score)


def measure(root, config, state, name, inputs, split, score):
    if state["evaluations"] >= config["maxEvaluations"] or state["elapsedSeconds"] >= config["maxSeconds"]:
        raise LabError("evaluation/time budget exhausted")
    state["evaluations"] += 1
    state["pending"] = {"id": name, "split": split, "startedAt": time.time()}
    save(root / "state.json", state)
    started = time.monotonic()
    if score is isolated_score:
        result = score(
            root / "snapshots" / f"{name}.py",
            inputs,
            timeout=min(5, max(0.05, config["maxSeconds"] - state["elapsedSeconds"])),
        )
    else:
        result = score(root / "snapshots" / f"{name}.py", inputs)
    validate_score(result)
    state["elapsedSeconds"] += time.monotonic() - started
    state["pending"] = None
    return {**result, "split": split}


def validate_score(result):
    if (
        not isinstance(result, dict)
        or type(result.get("total")) is not int
        or result["total"] <= 0
        or type(result.get("passed")) is not int
        or not 0 <= result["passed"] <= result["total"]
        or type(result.get("seconds")) not in (int, float)
        or not __import__("math").isfinite(result["seconds"])
        or result["seconds"] < 0
        or result.get("status") not in ("scored", "timeout", "execution_failed")
    ):
        raise LabError("invalid scorer result")


def finish_holdout(root, config, state, hidden, score):
    for row in state["results"]:
        if "holdout" not in row:
            row["holdout"] = measure(root, config, state, row["id"], hidden["cases"], "holdout", score)
            save(root / "state.json", state)
    state["phase"] = "complete"
    save(root / "state.json", state)
    return publish(root, config, state, hidden)


def publish(root, config, state, hidden):
    root = Path(root)
    result = report(root, config, state)
    candidates = root / "results" / "candidates"
    candidates.mkdir(exist_ok=True)
    for row in state["results"]:
        shutil.copyfile(root / "snapshots" / f"{row['id']}.py", candidates / f"{row['id']}.py")
    save(root / "results" / "config.json", config)
    save(root / "results" / "report.json", result)
    # Final release permits exact reproduction, but invalidates this holdout for future selection.
    save(
        root / "results" / "released-holdout.json",
        {
            "schema": SCHEMA,
            "generator": EVALUATOR,
            "seed": hidden["seed"],
            "randomCases": 100,
            "status": "released_after_selection_do_not_reuse_as_unseen",
        },
    )
    return result


def report(root, config=None, state=None):
    if state is None:
        config, state, _ = load(root)
    rows = json.loads(json.dumps(state["results"]))
    # No partial holdout results or hidden seed enter public progress reports.
    if state["phase"] != "complete":
        for row in rows:
            row.pop("holdout", None)
    return {
        "schema": SCHEMA,
        "evaluator": EVALUATOR,
        "configHash": state["configHash"],
        "holdoutHash": state["holdoutHash"],
        "phase": state["phase"],
        "stopped": state["stopped"],
        "generation": state["generation"],
        "evaluations": state["evaluations"],
        "elapsedSeconds": state["elapsedSeconds"],
        "selections": state["selections"],
        "results": rows,
        "config": config,
        "credits": None,
        "cost": None,
        "limitations": [
            "Artifacts are produced separately through normal approved development workflows.",
            "Evaluation time excludes development; tokens/credits are unknown without runtime records.",
            "Small synthetic task; no general quality improvement can be inferred.",
            "Public cases select parents; holdout scored once after all selections.",
            "Released holdout cannot be reused as an unseen benchmark.",
            "Candidate execution is serial and bounded; maxConcurrent is an upper bound.",
        ],
    }


def control(root, action):
    root = Path(root).absolute()
    with slots.FileLock(str(root / "lab.lock"), "lab"):
        config, state, _ = load(root)
        if action == "stop":
            state["stopped"] = True
        elif action == "resume":
            state["stopped"] = False
        elif action == "recover":
            if state["pending"] is None:
                raise LabError("no interrupted evaluation")
            # The reserved attempt stays consumed. Never silently reset budgets.
            state["elapsedSeconds"] += max(
                0, min(config["maxSeconds"], time.time() - state["pending"]["startedAt"])
            )
            state["pending"] = None
        save(root / "state.json", state)
        if action == "resume" and state["phase"] == "prepared":
            prepare_generation(root, config, state["generation"])
        return report(root, config, state)


def replay(root, score=isolated_score):
    """Reproduce published artifacts, without treating released data as unseen."""
    root = Path(root)
    published = read(root / "report.json")
    released = read(root / "released-holdout.json")
    if (
        type(published.get("schema")) is not int
        or published["schema"] != SCHEMA
        or published.get("phase") != "complete"
        or published.get("evaluator") != EVALUATOR
    ):
        raise LabError("completed report for this evaluator required")
    config = config_check(published["config"])
    if (
        published.get("configHash") != hashed(config)
        or (root / "config.json").is_file()
        and read(root / "config.json") != config
    ):
        raise LabError("published configuration hash mismatch")
    expected = {
        name for generation in range(1, config["generations"] + 1) for name in slot_names(config, generation)
    }
    rows = published.get("results")
    if (
        not isinstance(rows, list)
        or len(rows) != len(expected)
        or any(not isinstance(row, dict) for row in rows)
        or {row.get("id") for row in rows} != expected
    ):
        raise LabError("published candidate set incomplete or invalid")
    if (
        not isinstance(released, dict)
        or type(released.get("seed")) is not int
        or released["seed"] < 0
        or released.get("generator") != EVALUATOR
        or released.get("randomCases") != 100
    ):
        raise LabError("invalid released holdout metadata")
    inputs = cases(released["seed"], 100)
    if hashed(inputs) != published.get("holdoutHash"):
        raise LabError("released holdout does not match report")
    outcomes = []
    for row in published["results"]:
        validate_score(row["holdout"])
        if row.get("id") not in slot_names(published["config"], row["generation"]):
            raise LabError("invalid replay candidate id")
        candidate = root / "candidates" / f"{row['id']}.py"
        if candidate.is_symlink() or hashed(candidate.read_bytes()) != row["codeHash"]:
            raise LabError("published candidate hash mismatch")
        outcome = score(candidate, inputs)
        validate_score(outcome)
        outcomes.append(
            {
                "id": row["id"],
                **outcome,
                "matchesPublishedScore": (outcome["passed"], outcome["total"])
                == (row["holdout"]["passed"], row["holdout"]["total"]),
            }
        )
    return {
        "schema": SCHEMA,
        "mode": "released_data_replay",
        "results": outcomes,
        "reproduced": all(value["matchesPublishedScore"] for value in outcomes),
        "credits": None,
        "cost": None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").add_argument("--config", required=True, type=Path)
    for action in ("evaluate", "status", "stop", "resume", "recover", "replay"):
        sub.add_parser(action)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, read(args.config))
        elif args.command == "evaluate":
            result = evaluate(args.root)
        elif args.command == "status":
            result = report(args.root)
        elif args.command == "replay":
            result = replay(args.root)
        else:
            result = control(args.root, args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        parser.exit(1, f"lab: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
