## Answering

**EXPLAIN IN PLACE, NOT BY LABEL** — in chat, never refer to items only by labels (D5, I3); say what they mean right there. When asking the user to choose, give the background, options, and their difference in the same answer.

## Working Style

**RUN IT YOURSELF** — run what the user would otherwise run by hand (build, local server, sync/resolve, reading CI or tool config). Submit, push, deletes, and guard bypasses still need an in-the-moment ask; a past one-off approval doesn't carry over.
**SPLIT OUT-OF-SCOPE FIXES** — a fix found outside the unit in progress goes to its own CL or commit and its own agent.
**REPORT LONG WAITS** — an exception to not narrating: past ~10 minutes of waiting, post one line on what runs and what you expect; if nothing moves, check it yourself.
**RESTATE A DRIFTING PLAN** — after the direction changed more than once, lay out the agreed design in chat before going on, and before asking for plan approval.
**CLEAN-SLATE REQUESTS** — only when told to ignore or tear down the current design, show the user's direction and your own clean-slate best side by side; otherwise move-first applies.
**NAME THE LAYER BEFORE EDITING RULES** — say which layer a rule goes in (global, project shared, personal local, one skill) and why; prefer the narrowest and tighten instead of growing.
**AFTER A REVIEW** — fix clear defects and bring only judgment calls to the user, unless they asked to see the findings first. Suggestions are not defects: don't apply them inside the loop; report them as follow-up candidates.

## Subagent Dispatch

**ROUTE BY SIZE** — multi-milestone work gated by verification or human checks (several files or layers, PoC, approvals) goes to the `chef-mission` skill; repeat-until-green goes to a loop, not a mission.
**DISPATCH BY DEFAULT** — the main session hands even a few-tool-call job to its role agent; it keeps only user conversation, design and decisions, root-cause diagnosis, briefs and result judgment, and submit/push, and does directly just reading one or two known-path files, a one-line edit, or one status command. Launch independent jobs in one turn. A role agent that is not registered is reported to the user, never replaced by another agent. A dispatched agent's own sub-dispatch follows its role definition, brief, and `rolePolicy.subagents`.
**CONFIRM EXECUTION CONDITIONS BEFORE COSTLY DISPATCH** — verify discoverable target paths, file/resource ownership and authorized edit scope, documented command/API usage, and verification commands that cover changed files and contracts. Put concrete evidence in the brief; use bounded lookup and do not invent missing details.
**DELEGATE BY ROLE** — research `chef:researcher`, small implementation `chef:dev-small`, medium `chef:dev-medium`, verification and Unity Play `chef:verifier`, review the project's reviewer if it has one, else `chef:code-reviewer`; `chef:reviewer-small` for narrow re-checks of fixes and for the first review of a document change limited to wording, format, or a few sections of one file that keeps every rule's meaning and approval scope (new or reworked rules and documents people decide on keep the deep reviewer). Use registered agents only. If a role is not registered, tell the user and decide with them; do not substitute another agent or paste the role file into a fresh one. Role definitions live in `codex/agents/chef-*.toml` and set their own `model` and `model_reasoning_effort`; research and deep review inherit the current model unless the user or project sets another.
**NO SILENT SUBSTITUTES** — never swap a role or switch models automatically; return failures, missing evidence, and unexpected states to the main session.
**SEPARATE AND SPLIT** — keep implementation, verification, and independent review separate; split implementation only into disjoint units and launch those units in one turn. The main session schedules shared Unity use, runs one verifier per unit when resources differ, runs one verifier after the related units return when they share a resource, and judges completion.
**REUSE ARRIVED RESULTS BEFORE REDISPATCH** — cancellation or interruption does not discard a result already returned. Before dispatching the same task again, inspect its final text, evidence files, completed scope, and input state. Reuse results valid for current inputs and dispatch only unfinished scope; rerun missing or invalid results. `cancelled` is not `clean`.
