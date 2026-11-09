# Mission entry event

`mission_entry.py` runs before an adapter is required and before an `open` or `resume` marker.
Claude, Codex and Grok use the same implementation in `hooks/mission_entry.py`.
The event can prepare a discoverable adapter, continue in the current project, request a choice,
or delegate to an existing registered slot. It does not open or move an ACTIVE mission.
An existing adapter is inspected to identify slots and preserve ACTIVE. A damaged existing adapter fails closed
before the callback; missing-adapter bootstrap does not authorize automatic repair of an unsafe existing context.

## CLI

```text
python <plugin-root>/skills/mission/scripts/mission_entry.py --runtime claude --project <cwd> --mode new --session <session-id> --mission <mission-id> --task <goal>
python <plugin-root>/codex/skills/chef-mission/scripts/mission_entry.py --project <cwd> --mode resume --session <session-id> --mission <mission-id>
python <plugin-root>/grok/scripts/mission_entry.py --project <cwd> --mode new --session <session-id> --mission <mission-id> --task <goal>
```

`--project` is the actual working directory of the following marker, including a subdirectory when applicable.
Use the current runtime's real session id. A new mission uses its proposed id; resume uses the existing mission id.
Exit 0 returns one JSON object on stdout. Exit 3 returns `needs-input` and requires a user choice.
Other failures use exit 1 and stderr. Slot launch diagnostics go to stderr.

- `continue`: inspect the returned `project` and `adapter`, read the adapter and its project guidance, then run the matching standalone marker.
- `delegate`: `launched: true` confirms that the existing slot launcher accepted the launch. Stop work in the parent; the slot session runs entry again.
- `needs-input`: show `message` and stop. No receipt is issued.

An entry success does not prove that hooks are enabled, that a launched runtime has finished starting,
or that project guidance was reloaded into an existing session. The marker's hook response and ACTIVE ownership still need verification.

## Explicit external registration

The existing runtime-specific registry resolver selects the directory: `HAN_ADAPTERS_DIR`, the runtime's
existing configuration fallback, or the source checkout's `adapters` directory. Entry reads
`<registry>/mission-entry.json`. Keep this file, external commands, their private configuration and receipts outside the installed plugin tree.
Updates replace plugin files and preserve this external directory.

This is a fictional configuration. Replace every path with the operator's explicit selection.

```json
{
  "schema": 1,
  "events": [
    {
      "id": "workspace-entry",
      "roots": ["D:/Sandbox/ExampleWorkspace", "D:/Sandbox/ExampleSlots/*"],
      "allowedRoots": ["D:/Sandbox/ExampleWorkspace", "D:/Sandbox/ExampleSlots/*"],
      "trusted": true,
      "command": ["C:/Tools/Python/python.exe", "D:/LocalExtensions/workspace_entry.py"],
      "timeoutSeconds": 120,
      "watchFiles": ["D:/LocalExtensions/workspace_entry.py", "D:/LocalExtensions/settings.json"]
    }
  ]
}
```

`roots` selects which invocations run the event and require receipts. Matching reuses the project registry's
path-part globs and ancestor matching: a root also covers its descendants. Register narrow roots when sibling
projects need different entry behavior. Exactly one event may match. With no matching registration, existing
adapter discovery and marker behavior remain available without a receipt. A matching invalid or untrusted registration fails closed.

`allowedRoots` limits both returned project roots and actual delegated slot paths. Its default is `roots`.
It does not select invocations. A route outside this list fails; it does not become an unregistered fallback.
The returned adapter must equal the adapter independently rediscovered for that project and runtime.

`trusted: true` is a manual operator decision. Chef never discovers or approves executable project scripts.
`command` is a nonempty argv array, executed with `shell=False`; its executable must be an absolute existing file.
Existing file arguments must be absolute and included in `watchFiles`. Watch files must be existing absolute
paths. Executables and existing file arguments, including the extension script and its private configuration,
must remain outside the installed plugin. Additional read-only public inputs may be watched inside the plugin
so an update to those inputs invalidates an earlier receipt. Watching a public input does not authorize it as an external command.
Include the extension source and every imported helper or configuration file whose change should
invalidate a previous entry. Registration and watch-file content hashes bind receipts; the executable is bound
by its resolved path, size and modification time, which also supports Windows application execution aliases.
`timeoutSeconds` defaults to 120 and accepts integers from 1 through 3600.

## Event input and output

The command receives exactly one UTF-8 JSON object on stdin. Its working directory is `cwd`.

```json
{
  "schema": 1,
  "event": "beforeMission",
  "runtime": "codex",
  "cwd": "D:/Sandbox/ExampleWorkspace",
  "mode": "new",
  "sessionId": "current-session",
  "missionId": "20261109-sample-task",
  "task": "Create a sample tool",
  "isSlot": false
}
```

`runtime` is `claude`, `codex` or `grok`; `mode` is `new` or `resume`.
Do not change the existing ACTIVE or resume adapter binding. A subprocess cannot modify its parent's
environment or an already constructed slot configuration. Bootstrap must write the explicitly chosen project
adapter or external registry before returning. The parent rereads discovery, the adapter and `local.json` afterward.

Return exactly one JSON object on stdout; write diagnostics to stderr. The accepted results are:

```json
{"schema":1,"action":"continue"}
```

```json
{"schema":1,"action":"continue","project":"D:/Sandbox/ExampleWorkspace","adapter":"D:/Sandbox/ExampleWorkspace/.claude/orchestrator/adapter.json"}
```

```json
{"schema":1,"action":"delegate","project":"D:/Sandbox/ExampleWorkspace","slot":"a"}
```

```json
{"schema":1,"action":"needs-input","message":"Choose a workspace before continuing."}
```

`continue.project` must contain the invocation directory. Optional route fields must agree with independent
adapter discovery. Missing adapter discovery after the event is an error.
Registered `continue` resume requires the requester session id and runtime to equal the existing mission owner;
the guard checks these identities again. A new requester cannot acquire that mission by obtaining a fresh receipt.
Registered open and resume atomically store the optional `runtime` field in ACTIVE alongside its existing fields.
Older ACTIVE records without runtime require an existing session transcript that establishes the runtime;
missing or ambiguous evidence is an error. Slot launch and direct entry use the same session-evidence reader.
An existing matching transcript path recorded in `sessions.tsv` has priority. Without one, strict registered
entry requires exactly one fallback transcript across all runtimes and rejects duplicate candidates within a runtime.
Unregistered readers retain their existing first-match fallback.
Roots without a matching registration retain the legacy explicit resume behavior.
Delegation validates the target's adapter and slot name, then calls the existing `slots.py run` in the same runtime.
New delegation uses the CLI task; resume uses `--resume`, requires the target ACTIVE id to match,
and retains the recorded session and its runtime. It never opts into `--new-session`.
A slot invocation cannot delegate again. Existing missions in the parent are not moved, closed or delegated.
An entry with an existing ACTIVE preserves its project, adapter, local configuration and state directory in both modes.
Reentering `new` with that ACTIVE also preserves its session and recorded runtime.

Slot binding and generated launch guidance belong in `slots.beforeRun`, which runs under the canonical slot lock
before runtime launch. The entry event handles routing and bootstrap. An event failure, timeout, invalid JSON,
invalid route or failed `beforeRun` prevents a new runtime launch and entry does not create ACTIVE.

## Receipt and guard

A successful registered `continue` creates:

```text
<registry>/mission-entry-receipts/<runtime>/<sha256(binding)>.json
```

The binding contains the canonical invocation cwd, runtime, mode, session id and mission id.
Receipts also bind the full registration, explicit watch files, executable identity, discovered project,
adapter, adjacent `local.json` content and the pre-marker ACTIVE ownership. They expire after 600 seconds. Rerunning entry removes the previous
receipt for that binding before executing the event, so a later failed callback cannot reuse an old success.
Concurrent CLI entries with the same binding serialize callback execution, receipt replacement and failure invalidation
under a receipt-specific lock. This lock is separate from the canonical slot lock used by `beforeRun`.
Changing registration, extension source, watched configuration, adapter/local content or discovered route
requires another entry. A receipt for `new` cannot authorize `resume` or another session, mission, runtime or cwd.

For a matching registration the `open`/`resume` guard checks the receipt before adapter discovery and rejects
missing or invalid receipts even when no adapter exists yet. Mixed markers cannot bypass this check.
Registration matching canonicalizes cwd through symlinks and Windows junctions in both the CLI and guard.
The guard rechecks receipt, owner and adapter state binding under a lock keyed by the real ACTIVE path,
then applies its existing ACTIVE transition while holding that same lock. Runtime and registry locations cannot split this lock.
Lock contention rejects the marker immediately, and the hook never launches the external preparation command.
Close markers and roots without a matching registration retain their existing behavior.
Receipts are local workflow evidence, stored under the same user's control as adapters and hooks.
