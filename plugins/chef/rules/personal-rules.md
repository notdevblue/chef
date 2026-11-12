## Basic Attitude

**KEEP COMPANY RULES OUT OF CHEF** — never put company, customer, or private-project instructions or data in this public plugin. Do not copy, summarize, or anonymize them into shared rules, skills, agent instructions, examples, or plugin feedback candidates. Keep them in authorized private project guidance or local adapters.

**DO NOT DECIDE ALONE** — ask first when an instruction is ambiguous, a needed detail is unspecified, or a change touches an approved column, model, name, or flow. A choice that only avoids a clash (a free protocol number, a sync, a resolve) needs no ask: make it and report it. **DO NOT WRITE CODE WITHOUT FIXED DIRECTIONALITY.**
**NEVER REVERSE UNDER PRESSURE ALONE** — a user's question means re-check the rules and reasoning, not capitulate; if the position holds, keep it and say why.
**READ A RESTRUCTURE REQUEST AS A MOVE FIRST** — restate "make A into B" as what moves where in the existing shape (a key, a column, a responsibility). If that explains it, stop; don't widen it into a new entity or system, even if a reference project has one by that name.
**CHECK WHAT WAS ACTUALLY REJECTED** — before inheriting a rejection, state in one line what was rejected and what was never evaluated; rejecting one means doesn't reject its category. When access, permission, or visibility decides the design, run the check instead of trusting a summary.

## Code Writing Rules

**FOLLOW EXISTING CODE** — before writing or diagnosing, match nearby naming and structure and search for code that already solves the same problem; reuse or extend it instead of adding a new pattern or a divergent duplicate.
**NULLABLE, NULL RETURN IS PROHIBITED** — no nullable fields, parameters, or return types and no `null` returns, except in `Find*` or `TryGet*` methods. Default to `new()` or an empty value.
**FIX RULE-VIOLATING CODE FIRST** before adding new code — only within the function/block being modified; in a review-fix round, only within the lines the fix touches.
**NO COMMENTS** — make the code self-explanatory; fix a name or structure instead of commenting around it. Never narrate what or how the code does, even as rationale. Only a *why* the code can't express (non-obvious intent, external contract, hidden invariant, consequence, workaround reason) is allowed, in one short clause.
**ONE-VALUE CHECKS USE `if`/`else`** — compare against a single value with `if` plus `else`, not `switch`, preferring `==` over `!=`. Keep a do-nothing branch as an empty block with a one-clause reason comment. No ternary; `Math.Min`/`Max`/`Clamp` to pick a value is fine.
**BRANCH-VARYING VALUES GO INTO A FUNCTION** — move a per-branch value into one shared function named for the value, handling its edge cases; copying the check into every branch to drop the variable is not a fix.
**NO CIRCULAR CHANGE UNITS** — one-way dependencies between CLs or commits are fine (land the prerequisite first, state the order); changes that can't build or pass checks without each other share one unit. Confirm a new unit's symbols exist in head, not only in another pending unit.

## Root Cause First

**FIX ROOT CAUSES, NOT SYMPTOMS** — before coding a bug fix, explain why it could happen at the design level and confirm the fix removes that cause. If you can't explain why, say so and ask; do not write code yet. When a tool stalls or fails on some input, shrinking or rerouting the input to dodge it is a workaround, not a fix; if you need one now, say it is a workaround and hand off the tool's own fix as a separate unit in the same turn.
