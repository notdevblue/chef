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

The native plugin registers `chef_slot`, `chef_mission`, `chef_worker_status`, `chef_delegate`,
request middleware and mission hooks. Hermes builds with the public subagent
lifecycle API also get `chef_research_read` and the child lifecycle hooks. Its five
skills are `chef:mission`, `chef:slots`, `chef:adapter-init`, `chef:doctor`,
`chef:distill`. Namespaced skills are loaded with `skill_view`, not a flat skill
directory. Keep the checkout available for the lifetime of the running process.

Python 3.11+, local filesystem permissions, project tools and a project adapter
are required. Use `.hermes/orchestrator/adapter.json`, or the explicit
`HAN_ADAPTERS_DIR` registry. Hermes-specific overrides go in `runtimes.hermes`;
machine-specific paths and commands belong in excluded `local.json`. The Hermes
home registry fallback is `chef/adapters` under the active request profile's
home. In a standalone CLI process it is under `$HERMES_HOME`.

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
4. Call `chef_worker_status` before opening the mission. Follow the configured
   mission-entry checks and call `chef_mission(action="open", mission="<id>")`. For external slots,
   the handler acquires the reservation before initialization or workspace commands.
   Every tool invocation uses explicit task context.
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
Optional `readOnlyCommands` in the local external-slot settings lists exact argv for trusted, read-only
terminal commands after a successful close, such as `[["/opt/example/bin/workspaces", "status"]]`.
Use an absolute executable and explicitly list the arguments. Supply a single foreground command
and the absolute path of the released slot or its manager in `terminal`'s `workdir`. Use literal POSIX
shell quoting as emitted by Python's `shlex.join`. Chef checks ACTIVE, reservation journals, protected
commands and adapter deny rules. A read in the released slot also checks availability with the provider.

The provider runs from the manager with a schema-1 JSON request on stdin. Common fields are `action`,
`project`, `workspace`, `name`, `stateDir`, `data` and `clean`. Actions are `status` (inventory and
availability), `acquire` (new reservation), `check` (current reservation) and `release` (release the
same reservation). `acquire`, `check` and `release` also receive `who` and `key`. The provider must
verify its workspace identity and enforce the requested clean state. It returns only `{"ok":true}`
with exit 0 for success. The `status` action confirms that the slot is unreserved even when
`clean=false`. Acquisition must be exclusive, and any release must be atomic with the
ownership check. Providers should avoid logging identities or returning them in responses.

`chef_slot` checks availability and binds the task cwd. The native mission guard acquires the
reservation in the `chef_mission(action="open", mission="<id>")` handler, using the gateway's originating message ID and Hermes
session. The `chef_mission(action="close", mission="<id>")` handler returns the original reservation after worker completion, detaches that
session's tasks from the released slot and restores their prior working directories.
`chef_slot(..., dry_run=true)` reports `reservation: "UNLOCKED"` when the provider confirms availability.
Guard state is
kept outside the workspace in the Hermes profile. Existing reservations in the gateway home keep
their journal and lock until release. If both homes have a journal for the workspace, inspect both
before continuing. Unconfirmed transitions require operator recovery.
The coordinator and delegated workers check reservation ownership before tool use. This is a
cooperative reservation. The provider's own workspace rules still apply to tools used outside Chef.
Managed create, refresh and remove are unavailable for external workspaces.

## Role workers

Call `chef_delegate` to dispatch a role worker after the owned mission is initialized.
`chef_worker_status` reports the backend configured for the current workspace and the
roles it can dispatch under `agents.allow`. The selected backend and role limits are
checked before dispatch. No backend is silently substituted.

### Hermes profile backend

`hermes` is the default backend and supports researcher, dev-small, dev-medium,
verifier, code-reviewer and reviewer-small. It uses the active Hermes profile's
model routing and credentials. You can explicitly set a model or provider for an
individual role in the adapter or its `local.json` overlay:

Set leaf delegation in the Hermes profile so role workers cannot spawn more
workers. Disable inherited MCP toolsets so the child receives only its assigned
Chef tools:

```yaml
delegation:
  inherit_mcp_toolsets: false
  max_spawn_depth: 1
```

If the profile restricts enabled toolsets, allow `chef` for the parent session and
`chef-worker-read`, `chef-worker-write` and `chef-worker-shell` for worker roles.
Chef grants children only the role's required subset. `orchestrator_enabled: false`
can be used instead of `max_spawn_depth: 1` to prevent nested delegation.

```json
{
  "runtimes": {
    "hermes": {
      "workers": {
        "backend": "hermes",
        "hermesRoles": {
          "researcher": {"model": "demo-research", "provider": "sample-cloud"}
        }
      }
    }
  }
}
```

Values are passed as routing preferences to the Hermes worker API. They do not
change credentials. Hermes worker lifecycle support must be available in the
installed runtime. The worker's file and shell tools use the assigned native job
and Chef guards.

### Codex subscription workers

Select `codex` explicitly to dispatch through Codex CLI. Available roles are
researcher, dev-small, dev-medium, verifier,
code-reviewer and reviewer-small. They use the committed `codex/agents/chef-*.toml`
instructions and model/effort settings. Roles without a model override use the
user's existing Codex default; no Hermes model or paid API fallback is selected.

```json
{"runtimes": {"hermes": {"workers": {"backend": "codex"}}}}
```

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

### Claude CLI workers

Select `claude` explicitly to dispatch through the installed Claude CLI. All six
Chef roles are available. The default role model and effort come from the installed
Chef role definitions. Optional `workers.claudeRoles` entries can set a model and
effort for a role. Effort accepts `low`, `medium`, `high` or `max`.

```json
{
  "runtimes": {
    "hermes": {
      "workers": {
        "backend": "claude",
        "claudeRoles": {
          "dev-small": {"model": "demo-coder", "effort": "medium"}
        }
      }
    }
  }
}
```

Claude Code 2.1.295 or newer is required for fail-closed hooks. Dispatch also requires
a logged-in CLI account. Without an explicit settings snapshot, authentication comes
from the Hermes process environment, the selected `CLAUDE_CONFIG_DIR` and Claude's
credential store. Chef checks that the isolated worker resolves to the same auth route
and account. If known user or workspace settings select auth or provider routing, dispatch
stops and asks for an explicit snapshot instead of importing full settings.

Set `workers.claudeSettings` in a private adapter overlay to an absolute path to a
private JSON file. This snapshot accepts only `env`, `apiKeyHelper`, `awsAuthRefresh`,
`awsCredentialExport`, `gcpAuthRefresh`, `forceLoginMethod`, `forceLoginOrgUUID` and
`modelOverrides`. Values are limited to strings, string maps for `env` and
`modelOverrides`, and strings or a string list for `forceLoginOrgUUID`. The
`forceLoginMethod` values are `claudeai`, `console` or `gateway`, subject to the
configured CLI account policy. The `env` map cannot replace `CHEF_`, `HERMES_`,
`HAN_` or `PYTHON` variables. Other settings are rejected. Claude managed policy
remains controlled by the installed CLI.

```json
{
  "runtimes": {
    "hermes": {
      "workers": {
        "claudeSettings": "/home/example/.config/chef/claude-auth.json"
      }
    }
  }
}
```

Chef writes the selected auth fields and fixed per-phase worker permissions to a
private temporary settings file. The auth-status check, hook probe and assigned role
work use the same auth fields, workspace and process environment. Secrets stay out of
command arguments.
Each role launch disables nested agents and verifies the installed Chef hook with a
denied Bash probe before assigned role work.
Research and review roles have read tools only. Implementation roles can use guarded
write tools. Verifier shell use remains subject to the assigned job guards. These
CLI permissions and hooks are cooperative controls and do not provide OS filesystem
isolation.

### Named JSONL attachments

Use a named attachment for a model bridge that is not one of the built-in backends.
The bridge is a trusted local executable. Its configuration accepts only an absolute
executable path and literal arguments. No shell expansion or placeholders are used.
The bridge inherits the caller's authentication environment, so its owner must
review the executable and its credential handling.

```json
{
  "runtimes": {
    "hermes": {
      "workers": {
        "backend": "demo-bridge",
        "attachments": {
          "demo-bridge": {"command": ["/opt/demo/bridge", "--profile", "sample"]}
        }
      }
    }
  }
}
```

The process uses schema 1 JSONL on stdin and stdout. It first answers a `status`
request with supported Chef roles and `transport: "chef-tools-v1"`. Chef then sends
a `delegate` request with a host-generated task ID, role, prompt, assigned workspace,
timeout and allowed tool names. A tool request includes a unique request ID, tool
name and arguments. Chef executes it through the same native job and sends a matching
`tool_result` with the result or error. The bridge finishes with a `result` object.
Chef sends `cancel` after a failed or timed-out delegation.
Read-only roles receive only the read tool. Messages are limited to 1 MiB and a
delegation may make at most 1,000 tool calls. Invalid messages, duplicate request
IDs, timeout or lost job ownership fail the delegation and revoke job access.

This protocol mediates tool requests. The bridge process and its descendants remain
trusted local programs and the protocol is not an OS filesystem sandbox. The built-in
`claude` backend supplies the supported Claude CLI integration. Grok Build CLI has
no built-in backend. Live model execution, provider behavior and installation-specific
authentication still require testing in the target Hermes environment.

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

## Mission transitions and project contracts

Hermes uses `chef_mission` with `open`, `resume`, `close` and a mission ID. Use
`chef_mission(action="status")` to inspect the selected workspace's ACTIVE and
whether it belongs to this session. Shell mission markers are rejected in the
native Hermes bridge. Claude, Codex and Grok keep their existing marker contract.
Pre-tool hooks only validate native operations; the handler rechecks ownership
under the mission lock and then changes state, after every plugin has accepted
the call. A blocked close never removes ACTIVE. An absent ACTIVE alone does not
authorize another close or prove that an external reservation was released.

A trusted project can publish `workers.readOnlySources` as an object from
canonical absolute file paths to SHA-256 hashes. Only exact listed files are
readable outside the workspace; reads check the hash and reject symlinks,
non-text files and files above 2 MiB. There are no directory grants or external
write grants. These entries are project configuration, not model tool arguments.

`workers.roleContracts` maps a Chef role to `{path, sha256, modelPolicy}`. `path`
is a workspace-relative TOML with `developer_instructions`; `modelPolicy` is
`definition` or `inherit`. Native dispatch loads the pinned body into the child
context and applies a fixed model at launch, then verifies the returned model.
Inherited definitions must not retain a fixed model. Native Hermes workers use
the host's reasoning configuration, with no per-worker effort override. The
Codex TOML field `model_reasoning_effort` is not imported into Hermes routing and
does not block readiness. Codex and Claude backends keep their own explicit
settings. Source hashes and role contracts are checked by
`chef_worker_status` before opening a mission.
