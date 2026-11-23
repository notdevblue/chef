# Hermes on macOS and Linux

Chef runs inside the existing Hermes task with the native local terminal backend.
No desktop session, terminal window, AppleScript or tmux is needed. Remote,
container and SSH terminal backends are rejected because their paths do not
identify the host workspace. Windows Hermes is not part of this integration;
the existing Claude, Codex and Grok integrations remain available.

## Installation

Use Hermes with the native directory-plugin API (`plugin.yaml`, `register(ctx)`),
`pre_tool_call` blocking hooks, `tool_request` middleware, and namespaced plugin
skills. The integration was checked against Hermes v0.18.2. Older builds without
these APIs must be upgraded; a skill-only installation does not apply the guards.

Link `<checkout>/plugins/chef/hermes` to `$HERMES_HOME/plugins/chef` (the default
home is `~/.hermes`). Inspect any existing target before changing it. In the
Hermes config, add `chef` to `plugins.enabled`, preserving the other entries.
Restart the CLI or gateway process after changing the installation. Do not
enable project plugins or tool overrides for this integration.

If the CLI or gateway uses a restricted toolset list, include the `chef` toolset
for the intended surface. Registration does not override a disabled toolset.
Plugin-disabled/safe-mode sessions are not guarded Chef sessions.

The native plugin registers tools `chef_slot` and `chef_delegate`, hooks
`pre_tool_call`, `pre_llm_call`, `pre_verify`, request middleware, and five
explicit skills: `chef:mission`, `chef:slots`, `chef:adapter-init`, `chef:doctor`,
`chef:distill`. Namespaced skills are loaded with `skill_view`, not a flat skill
directory. Keep the checkout available for the lifetime of the running process.

Python 3.11+, local filesystem permissions, project tools and a project adapter
are required. Use `.hermes/orchestrator/adapter.json`, or the explicit
`HAN_ADAPTERS_DIR` registry. Hermes-specific overrides go in `runtimes.hermes`;
machine-specific paths and commands belong in excluded `local.json`. If present,
`$HERMES_HOME/chef/adapters` is the Hermes home registry fallback.

The native context supplies the absolute `Chef CLI Python executable` from the
running Hermes process, including before a project adapter or slot is selected.
Use that interpreter for Chef commands. A terminal's `python3` may resolve to an
older system Python even when Hermes itself uses a supported virtual environment.

## Current-session work

1. Load `skill_view(name="chef:mission")` and its linked shared procedure.
2. Prepare the configured slot with the existing `skills/slots/scripts/slots.py`
   entry point and `--runtime hermes`. The command supports `create`, `refresh`
   and `status`; Hermes `run` directs the caller to the native tool instead of
   opening a terminal.
3. Call `chef_slot(project="/absolute/project", name="slot1")`. `dry_run=true`
   only inspects; normal entry checks readiness/ownership and executes configured
   `beforeRun` under the slot lock. It binds the trusted task's working directory.
   It does not open ACTIVE or approve a plan.
   The selected slot must also resolve its own adapter through a local file or
   registry roots, with the same stateDir. Entry is rejected if its guards would
   have no project context.
4. Use the normal guarded mission-entry and marker procedure, then initialize
   and perform the mission. External entry registrations still apply. Every tool
   invocation uses explicit task context rather than relying on a previous `cd`.
5. For the same persisted Hermes session, call `chef_slot(..., resume=true)`
   after restart. Another live session cannot take over ACTIVE through this tool.
   Follow the existing inspected recovery procedure for an actual ownership change.

Slot bindings are process-local. Re-select a slot after restarting Hermes.
Do not release ACTIVE just because a chat turn ended. Native questions and
approval transport remain Hermes/gateway responsibilities; Chef never treats
silence, expiry, reconnects or worker output as user approval. This plugin does
not post messages or change Slack/channel routing.

## Externally managed workspaces

For native Hermes, a registry adapter can declare `runtimes.hermes.externalSlots` instead of managed
`slots`. Register the manager and each workspace as exact roots of the same adapter. In its local
overlay set `externalSlots.root` to the allowed parent directory, `provider` to an absolute executable
argv, and `workspaces` to a map from existing Chef slot names to `{path, data?}`. `path` is the existing
workspace's absolute directory. Optional `data` is an object passed to the provider. The workspace
must be inside `root`. Names follow the standard Chef slot rules and apply independently of the directory name.
Optional `protectedCommands` are case-insensitive regular expressions for provider operations that
the Hermes coordinator and its workers cannot run as shell commands, including through one-time overrides.

The provider runs from the manager with a schema-1 JSON request on stdin. Common fields are `action`,
`project`, `workspace`, `name`, `stateDir`, `data` and `clean`. Actions are `status` (inventory and
availability), `acquire` (new reservation), `check` (current reservation) and `release` (release the
same reservation). `acquire`, `check` and `release` also receive `who` and `key`. The provider must
verify its workspace identity and enforce the requested clean state. It returns only `{"ok":true}`
with exit 0 for success. Acquisition must be exclusive, and any release must be atomic with the
ownership check. Providers should avoid logging identities or returning them in responses.

`chef_slot` checks availability and binds the task cwd. The native mission guard acquires the
reservation on the standalone `open` marker, using the gateway's originating message ID and Hermes
session. The `close` marker returns the original reservation after worker completion. Guard state is
kept outside the workspace in the Hermes profile. Unconfirmed transitions require operator recovery.
The coordinator and delegated workers check reservation ownership before tool use. This is a
cooperative reservation. The provider's own workspace rules still apply to tools used outside Chef.
Managed create, refresh and remove are unavailable for external workspaces.

## Codex subscription workers

Role work uses `chef_delegate(role="chef:dev-small", prompt="...")`, not Hermes
`delegate_task`. Available roles are researcher, dev-small, dev-medium, verifier,
code-reviewer and reviewer-small. They use the committed `codex/agents/chef-*.toml`
instructions and model/effort settings. Roles without a model override use the
user's existing Codex default; no Hermes model or paid API fallback is selected.

Install Codex CLI and log in with `codex login` using ChatGPT. Install/update Chef
for Codex and explicitly trust its hooks in `/hooks`. Workers enable hooks but
never bypass hook trust, approval or sandbox controls. They pin the OpenAI
provider to ChatGPT authentication and refuse an API-key-only login.

An adapter can add project Codex settings to every worker and its probe with
`workers.codexArguments` (`"-c", "key=value"` pairs; usually under
`runtimes.hermes.workers`), for example `sandbox_workspace_write.network_access`.
Chef keeps the account, model, instructions, sandbox mode and writable roots, hook
feature, approval policy, hooks and MCP servers pinned; invalid or pinned keys fail before authentication.
Session-flag hooks need persisted hook trust that headless workers never get, so a
project check uses `workers.guardCommand` instead: Chef's trusted worker hook runs
that argv for every guarded worker tool call with the normalized payload (owner
Hermes session, agent id `chef-codex-worker`) on stdin. Any deny, unexpected output,
failure or timeout (8 seconds) denies the call. The command is fixed in the worker
context when the delegation starts, so an adapter the worker writes later cannot drop it.

Before a write-capable worker starts, a read-only probe must observe both a
matching installed Chef SessionStart hook and an actual denied PreToolUse call.
Missing/untrusted/outdated hooks fail the delegation. Model work is billed under
the user's existing subscription limits; the probe also consumes a small turn.
The owned mission must already be initialized (`mission_state.py init`); a bare
ACTIVE marker is rejected before any model turn is requested.
Workers run synchronously with a timeout and inherit the parent mission's guard
policy. Codex runs with `--no-daemon` so per-worker hook context and process
cleanup do not depend on a pre-existing shared server. Supported
mission-ownership and state-write operations are blocked.
Opaque build/helper programs remain a trust boundary: protected mission files
are compared before and after the worker, and unexpected changes fail the result
without restoring files automatically. This is detection, not an OS filesystem
isolation guarantee. Research/review roles use
read-only sandboxes; implementation/verification use workspace-write. Nested
Codex multi-agent tools are disabled. Worker return text is evidence to inspect,
not a completed unit, approval, or independent validation by itself.

## Limits and validation

Hermes `pre_verify` is an advisory, bounded continuation hook. It is not an
unconditional Stop veto; mission close and question-integrity checks remain
mandatory. Native SQLite conversations are not parsed as JSONL. Distill and
transcript commands need an explicit supported exported transcript. Hermes
observatory metadata is explicit, not inferred from another runtime's files.
The supervisor cannot create new Hermes sessions; current-task execution is the
supported path. Extra GUI terminal launchers and Lab sandbox portability are
outside this change.

CI runs the synthetic integration suite on macOS, Linux and Windows. Native
PluginManager loading and hook dispatch can additionally be tested against an
installed Hermes source in an isolated Hermes home. These checks do not prove
live provider behavior or an external chat platform's delivery/approval flow.
