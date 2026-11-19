# 실행 통계·작업 대기열·모델 선택 추천

Current scope (2026-11-17): **실행 통계 (Run Metrics)**, **작업 대기열·배정
(Task Queue & Dispatch)** and **모델 선택 추천 (Model Recommendations)**.
Lab is excluded from further development and experiments. Existing Lab code,
fixtures and historical results below are retained as records.

Canonical CLI entrypoints are `run_metrics.py`, `task_queue.py` and
`model_recommendations.py`; they delegate to the existing implementations.
The original `observatory.py`, `supervisor.py`, `routing.py`, `/observatory`
routes and `--observatory-report` flags remain supported. Internal module/class
names, report schemas, `CHEF_SUPERVISOR_HOME`, queue paths and saved data do not
change. The historical document path is also retained for existing links.

## Scope and architecture

Chef Core remains the mission engine. `shared/skills/mission.md` owns A/B/C approval,
independent verification and review; `mission_state.py` owns mission records;
`verification.py` owns reproducible execution receipts; `slots.py` owns workspace
preparation, VCS boundaries and locks. Distill only applies user-selected feedback.
The dashboard remains a localhost GET-only reader.

New measurement, scheduling and experiment tools live beside the existing mission
scripts. They do not write mission approval, question answers, ACTIVE or completion.
Role definitions and global model/effort defaults remain unchanged. No runtime
reset ticket is needed. Credit and billing totals are unknown unless authoritative
runtime evidence explicitly provides them; token counts are not credits.

The shared slot lock now holds a kernel advisory lock on a permanent `.gate`
sidecar for its full lifetime (`flock` on POSIX, a byte lock on Windows). This
fixes a reproduced race where two stale reclaimers could unlink a new live
holder's PID marker and both enter. The original PID marker path and stale/live
owner policy remain; the gate is never removed or replaced. Kernel locks release
on process death. Verification resource locks still require deliberate recovery
of their abandoned PID markers. Restart already-running workers before mixing
old and new lock implementations. Do not clean up `.gate` files while any caller
may use that workspace. This applies to all three runtimes through shared slots.

## Investigation and baseline (2026-11-16)

Starting revision: `88e53731a9a2590d7c2ed70efdbc7721cde5d0bd`; clean worktree.
Canonical `notdevblue/chef` main matched that revision. The configured origin still
used the previous repository name; deployment must target the canonical repository.

The code already implements Claude usage deduplication, activity mapping,
verification timing/failure/reuse records, structured review findings, Git/Perforce
and NoVcs slots, three runtime guards and read-only mission discovery. Reuse those
implementations. Missing capabilities were cross-runtime normalized reports,
Task Queue & Dispatch queues, controlled experiments, measured routing and CI.

Initial full suite on Linux/Python 3.13.18 in an isolated pytest environment:
**2309 passed, 7 failed, 10 skipped, 59 subtests passed** (98.72s).
`sync_shared.py` passed (5 shared skills, 15 entrypoints, 16 reference pointers).
The seven pre-existing failures concern Windows separators on POSIX, fail-open
listing of an invalid shared manifest, POSIX file URLs and normalized raw asset
URLs. They remain regression gates and are being fixed rather than excluded.
No Windows execution has been performed locally; the CI matrix provides that gate.

## Milestones and dependencies

| Step | Reuse and additions | Validation / exit evidence |
| --- | --- | --- |
| M0 investigation | Existing runtime, mission, slots, guards, verification, Distill and dashboard audit | Baseline above; source and public boundary review |
| M1 Run Metrics | Extend existing usage and quality readers with versioned, metadata-only runtime adapters and export | Partial/malformed logs, unknown coverage, deduplication, migration, privacy tests |
| M2 validation | Repair baseline portability defects; add isolated kill/contention/corruption tests and Windows/Linux CI | Full unchanged existing suite plus new tests; JUnit artifacts |
| M3 Task Queue & Dispatch | Persistent bounded queue, dependency/priority selection, registered ready slots, durable reservations; existing slots launcher only | Conflict, launch ambiguity, recovery and approval-boundary tests |
| M4 routing | Compare observed success/latency/usage within matched task conditions | Coverage and uncertainty tests; recommendations only, no config mutation |
| M5 Lab | Synthetic projects in existing NoVcs slots, immutable candidate artifacts, sealed holdout evaluation, bounded generations | Reproducible full experiment, hashes, baseline comparison and honest limitations |
| M6 integration | Dashboard GET reports, three runtime documentation, naming compatibility, deployment | Full suite, public diff/history review, versioned commit/push, installed-content checks |

M1/M2 are independent. M3 uses the existing core, then M4/M5 consume M1 results.
M6 follows integration and independent review. Public examples and fixtures are
created from scratch; no private adapter or real project is an experiment input.

## Safety and remaining design constraints

`slots run` returning success proves launch request delivery, not child startup.
Task Queue & Dispatch must reserve before launch, wait for evidence and retain uncertain
reservations for operator review. It must never retry an ambiguous launch or take
over a session. Verification resource locks intentionally fail closed after owner
death; report them for deliberate recovery instead of deleting them.

Mission approvals are procedural contracts, not just state labels. The Task Queue & Dispatch
must enter through normal runtime mission workflows and cannot manufacture
approval by editing a mission header. Dashboard reports never execute or approve.

Lab holdout cases must stay outside candidate slots and are only scored after
candidate inputs are frozen. Execution of arbitrary candidate code requires an OS
isolation boundary. Fixture-only machinery checks do not demonstrate AI quality.
Report requested and observed model settings separately; no provider access or
credit consumption is inferred from configured role names.

## Progress and resume

M0–M5 implemented and tested. M6 integration and Codex/Grok deployment are verified;
Claude installation verification remains unavailable because its CLI is absent.
The live experiment and reproducible artifacts are in
[`examples/lab/interval-union`](../examples/lab/interval-union/README.md).
Four independent candidates all passed 26 public and 106 holdout cases; the two
independent reviews found no defects. No improvement was demonstrated. Replay
reproduced every score. The live run used one generation; five-generation traversal
is separately tested with deterministic fixture scorers.

## Commands and operational boundaries

All commands below refer to the shared `plugins/chef/skills/mission/scripts/` folder.
They work identically from Claude, Codex and Grok shells; no new native roles,
skills or hooks are registered. Existing role definitions and the eight Grok
skills remain unchanged.

### Run Metrics

```bash
python run_metrics.py --source claude=<session.jsonl> --source codex=<rollout.jsonl> --source grok=<updates.jsonl> --output <report.json>
python run_metrics.py --source manual=<metadata.jsonl> --output <report.csv> --format csv
```

Run these using the full shared script path or from its directory. Sources are
explicit files; there is no whole-home scan or upload. An optional activity map
uses the existing `usage_report.py` map contract (`--activity-map`). Add
`--project name=<project> --mission-root name=<missions> --start <ISO> --end <ISO>`
to reuse existing verification/review/ledger readers. Exported records omit source
paths and transcript bodies. Metadata labels may themselves be private; keep
reports outside public Git. JSON `null` means **unknown**, never zero usage.

| Runtime | Native observed evidence | Not assumed |
| --- | --- | --- |
| Claude | Assistant usage maxima per request/message, recorded model/effort, separate Advisor tokens | Role from model name; missing counters; billing/credits |
| Codex | Explicit rollout session/turn context and cumulative token deltas | Quota consumption, cost; counters missing from logs |
| Grok | Native ACP turn completion activity | Unverified native usage/model/effort schemas; token/cost/credits remain unknown |
| Explicit instrumentation | `schema:1`, `kind:runtime_metadata`, strict metadata fields | Values absent from real caller evidence |

Manual JSONL records use `runtime`, `sessionId`, `agentId`, `timestamp` (timezone
ISO), `project`, `mission`, `unit`, `phase`, `taskKind`, `role`, `model`, `effort`,
`requestedModel`, `requestedEffort`, `durationSeconds`, `retries`, `outcome`
(`unknown|success|failure|interrupted`), `failureCategory` and `tokens`.
Token keys: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `reasoning_output_tokens`, `total_tokens`.
Fields may be omitted and are unknown; no raw content fields are accepted.
Execution failure rates use only explicit success/failure outcomes; token events
alone cannot establish execution success. Interrupted and unknown counts stay separate.
Do not sum total/cache/reasoning with input/output: some counters overlap.

Schema 2 reports validate on import/export and recompute summaries. Schema 1
legacy usage reports migrate in memory to aggregate evidence with **unknown
coverage**; no fabricated per-response records or historical zero certainty.
Original files are preserved. Newer unknown schemas fail closed. CSV exports
escape spreadsheet formula prefixes. The dashboard `--run-metrics-report` option
adds GET-only HTML/JSON/CSV views at `/run-metrics` through the same serialization layer; requests
do not run collectors, launch tasks, save answers or change files.

### Task Queue & Dispatch

Use one explicitly configured authority home for all supervising runtimes:
`CHEF_SUPERVISOR_HOME` (absolute directory), default `~/.chef/supervisor`.
Only `<home>/queue.json` is accepted as `--state`; this prevents two queue files
from independently launching the same slot during the launch/ACTIVE gap. All
supervised projects must be in that queue's single config. Config changes need a
deliberate new bounded queue; do not reset a live queue or use different homes
for the same slots/resources. Back up config/queue before operator recovery.

Config schema 1 contains `projects` keyed by a fictional/user-selected name;
each value has exactly `path` (existing absolute main project), `slots` (registered
slot names), `runtimes` and `resources` (shared resource names). `budgets` has
positive integer `maxRuns`, `maxConcurrent`, `maxSeconds`, `launchTimeoutSeconds`.
No command template or executable supplied by a task is accepted.

```bash
python task_queue.py --config <config.json> --state <home>/queue.json enqueue task-a --project sample --goal 'Implement the approved task' --priority 10
python task_queue.py --config <config.json> --state <home>/queue.json enqueue task-b --project sample --goal 'Verify the approved follow-up' --after task-a
python task_queue.py --config <config.json> --state <home>/queue.json plan
python task_queue.py --config <config.json> --state <home>/queue.json dispatch --execute
python task_queue.py --config <config.json> --state <home>/queue.json reconcile
```

Without `--execute`, dispatch is a proposal. Execution invokes only existing
`slots.py run --runtime …`, which retains preparation, guards and the mission
workflow. Readiness uses the same configured launcher preflight as slots: the CLI,
terminal executable and required GUI session must be available. Windows defaults
to Windows Terminal; POSIX defaults to interactive foreground. Unattended dispatch
requires an explicit GUI terminal or tmux backend on POSIX; it cannot use foreground
or create Hermes tasks. Successful launcher invocation is not mission entry or success of work.
Reconcile must observe the new ACTIVE ownership and then validated closed/C-approved
mission completion, done units, receipts and independent reviews before freeing
reservations or satisfying dependencies. A fast mission not observed as ACTIVE,
corrupt state, interruption or ambiguity produces `needs-operator`. This retains
resources and never closes/resumes/retries automatically. Operator recovery uses
the existing mission workflow and deliberate queue correction after backing up;
no unattended override endpoint exists. Time budgets stop future dispatch; they
do not kill a child mission. The queue is local metadata, not an authorization token.

### Model Recommendations

`model_recommendations.py --run-metrics-report <comparison.json> --baseline <name> --output <recommendations.json>`
compares explicitly matched execution records. Records require `conditionKey`, `strategy`
and boolean `success`; ordinary token-only Run Metrics records cannot imply task
success. The report stratifies runtime/model/effort/role/strategy, shows candidate
sample counts, Wilson intervals, latency and token coverage, and preserves unknown
usage. Fewer than five independent candidate outcomes per condition defaults to
insufficient evidence. Test cases are not independent agent runs. Fixture and
unmatched comparisons cannot recommend a quality improvement. No role/model/effort
configuration is written; retaining old defaults is also the rollback.

### Archived Lab (excluded from further work)

`lab.py --root <new-root> prepare --config <config.json>` creates a fresh synthetic
project and ordinary NoVcs slots. Config schema 1 specifies `generations` (1–10,
use 5–10 for repeated studies), `candidates` (4–20), `seed`, `maxEvaluations`,
`maxSeconds`, `maxConcurrent`, `costPolicy: unknown-no-paid-launches` and one
strategy per candidate (`name`, `kind: single|multi|fixture`, `runtime`, `model`,
`effort`). Evaluation budgets cover both development and final holdout batches.
Repeat an identical strategy name and settings across multiple candidate entries
to collect independent replications within one matched generation (for example,
five baseline and five alternative runs). Candidate slot IDs remain independent;
one strategy name cannot refer to conflicting settings. A single candidate per
strategy cannot satisfy the recommendation sample threshold by adding test cases.
Candidate execution is serial, below the concurrency ceiling. The first live
study config is included as a reproducible example.

Workers implement each generated `TASK.md` into its own `solution.py` through
normal approved development workflows. Lab coordinates **artifact evaluation and
parent selection**, not a new unrestricted provider execution path. This release
does not automatically launch or bill providers. Implementation/review call counts
and their development budgets must be bounded by the driving orchestrator; Lab's
evaluation budget measures scoring batches, not model calls. The initial live
study was driven by the coordinating session with four implementation and two
review calls; these are distinct from the eight scoring batches.

`evaluate` freezes all candidates before scoring, selects parents using public
cases, prepares the next generation and scores the sealed holdout only after all
generations finish. `status` hides partial holdout scores; `stop`/`resume` preserve
state. `recover` explicitly consumes an interrupted scoring reservation rather
than resetting its budget. Held-out inputs are generated/stored outside candidate
slots; arbitrary Python executes only on Linux inside bubblewrap with no network,
cleared environment, read-only mounts, process/CPU/memory/time/output limits.
Windows scoring fails closed instead of running candidate code unisolated.
Code hashes, config hash, holdout hash and evaluator identity support replay.
Final publication releases the seed, making it unsuitable for future unseen use.
The controller/evaluator filesystem must not be exposed to development agents;
this is an operator workspace boundary, not an OS isolation claim about a live
provider's tools. The first live workers were explicitly restricted to their slot;
automated evaluator execution itself has the tested OS isolation boundary.

## Compatibility and restoration

`chef-*` are public skill/role identifiers. `han-mission` lifecycle markers,
`HAN_ADAPTERS_DIR`, `HAN_REPO`, `HAN_TRANSCRIPT`, `HAN_DISTILL_LOG`, guard variables,
the `han` personal data directories and `han_state.py` internal implementation
remain compatible. New modules do not rename them or rewrite existing adapters,
mission records, transcripts or installed role defaults. Existing alias tests and
three-runtime contract suites remain gates. No old ignored adapter is force-added.

Remove the optional dashboard flag to return to its previous view. Stop invoking
the standalone tools to disable observation/dispatch/experiments. Back up private
queues and experiment roots before removing them; never delete a live reservation
or ACTIVE to disable supervision. Rollback plugin code through the normal plugin
installation process from a verified prior release; do not replace local records
with older report schemas. No new service or auto-start setting is installed.

## Verification evidence

Linux/Python 3.13: initial baseline above. Independent new suites verify metadata
privacy, unknown coverage, corrupted records, shared queue authority, durable
launch ambiguity, resource contention and killed processes. Lab tests exercise
five-generation fixture studies and actual bubblewrap host-data/network/write
isolation, runaway timeout and input mutation. Dashboard tests use a real HTTP
server and reject POST. All tests use synthetic temporary files; existing fake
Git/Perforce tests remain in the full suite.

Remote history review covered four branch refs, 55 commits, 333 distinct paths
and 932 distinct blobs, plus both historical PR/issue bodies and comments. Pattern
hits were the existing fictional `C:/Users/example` test path. No private adapter,
real workspace, transcript or Perforce server was used. This is a scoped review,
not a claim that automated pattern matching proves the absence of every secret.

After the lock fix, the full release suite passed **2499 tests, 10 skipped,
59 subtests** in 99.95s on Linux/Python 3.13.18 (2026-11-16).
Shared-contract checks passed (5 shared skills, 15 entrypoints, 17 reference pointers);
bytecode compilation and whitespace checks passed. The new lock regression uses real spawned processes,
preserves sidecar identity across killed-owner recovery and retains fail-closed
verification resource recovery. Final release tests and installation evidence
will be recorded below.

No reboot or relogin has been performed;
file-backed persistence and process-level tests are distinct from those checks.

The first remote matrix exposed three additional portability defects (2026-11-17):
Python 3.11 argparse treats an explicitly supplied empty string equal to its default
as absent in a required mutually exclusive group; inspection now uses `None` as
the absence sentinel. Windows Python 3.11 lacks native junction detection;
`is_link` now checks nofollow reparse attributes when that API is unavailable.
Git checkout newline conversion changed published candidate bytes and broke hash
verification; `.gitattributes` preserves exact candidate bytes and sets LF for
ordinary source files. Original tests remain unchanged, and the matrix is rerun.
The initial workflow's runner-context placement error was also corrected by
initializing runtime homes in a running step, following
[GitHub context availability](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability).

## Release verification (2026-11-17)

Version **0.4.1**, implementation/payload commit
`2ac39b7978003a762fcd462249b821fdf79219eb`, pushed to canonical `notdevblue/chef`
main; remote SHA equality checked. The earlier four implementation commits separate
locking/CI, Run Metrics/Task Queue & Dispatch, Lab/routing and runtime documentation.

[Full remote validation](https://github.com/notdevblue/chef/actions/runs/43892059282)
passed all four jobs: Windows/Linux × Python 3.11/3.13. Shared contract checks and
the complete existing/new pytest suite ran in each job; JUnit reports and step
summaries were published. Windows exercised the real junction guard and kernel
lock branch. Bubblewrap evaluation remains Linux-only; local real isolation and
published experiment replay passed. A fresh `core.autocrlf=true` checkout also
reproduced all four 106/106 holdout scores with identical code hashes. The affected
Python 3.11 local suites passed 210 tests; the junction/slot suites passed 433 tests
with 9 platform skips. Earlier complete local release validation is recorded above.

| Deployment surface | Observed result |
| --- | --- |
| Codex | Marketplace registration/upgrade then add succeeded; installed 0.4.1, all 170 tracked plugin files matched pushed payload bytes. Existing model, effort, provider and profiles unchanged. No native role directory was present, so none was created. |
| Grok | Existing native link already selected `<checkout>/plugins/chef`; no re-registration needed. Inspect confirmed 8 skills, 1 hook and the six actual role names/source paths, without watch/handoff. Manifest, skills and all shared payload files matched pushed bytes. |
| Claude | Both required update commands failed with exit 127 (`claude: command not found`). No Claude install/cache exists in this validation environment; installation and live runtime reflection remain **unverified**. |
| Dashboard | No pre-existing dashboard process was running. Tests started a real localhost HTTP server, read report JSON and verified POST rejection; no approval/run endpoint was added. |

Open new Codex/Grok sessions to read updated skill instructions and use the common
lock implementation; existing sessions were not restarted automatically. Verify
hook trust in the new session. Plugin files/marketplace configuration persist on
disk; a fresh CLI process listed the enabled Codex installation. This is not a
reboot/relogin validation. Private installation backup/restoration instructions
are maintained outside this public repository; no local configuration or cache
contents were copied here.

Remaining research scope is explicit: the included live-model study is one
generation with four candidates, not a five-to-ten-generation model tournament.
The multi-generation coordinator is tested using fixtures. Provider workers use
existing approved orchestration rather than a new autonomous paid-launch API;
development-agent filesystem access is an operator boundary. Native Grok usage,
actual billing/credit counters and unrecorded development efficiency remain unknown.
No global routing default was changed and no general quality improvement is claimed.

## Functional names (0.4.2, 2026-11-17)

README, bundled runtime guidance, CLI help and dashboard labels now use the three
functional names at the top of this document. The dashboard's canonical report
route is `/run-metrics` with JSON/CSV variants and `--run-metrics-report`;
existing routes and flags remain aliases. Canonical CLI files are thin entrypoints
to the same implementations, so no second queue, collector or recommendation
engine is introduced. Saved records and private configuration need no migration.
Lab code and historical fixtures are untouched and excluded from further work.

Validation: 286 existing metrics/queue/recommendation/dashboard tests passed,
including a real HTTP request and parity between new and legacy report routes.
All three new entrypoint help commands, both dashboard report flags, shared
runtime-reference checks and whitespace checks passed. These are naming and
compatibility checks; no new Lab experiment or model benchmark was run.
