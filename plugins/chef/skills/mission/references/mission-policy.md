# Mission transition policy

An adapter may set `guard.missionPolicyCommand` to a trusted, nonempty argv array.
For example, an independently authored fictional policy could be configured as:

```json
{"guard": {"missionPolicyCommand": ["python3", "/fictional/policy_check.py"]}}
```

The Python capability constant `chef.guards.policy.MISSION_POLICY_API` is `1`.
Chef sends the normalized tool payload as UTF-8 JSON on stdin, with the invocation
working directory, before an open, resume, or close marker changes mission state.
The command runs synchronously without a shell. It must only judge the request;
Chef remains responsible for ACTIVE and session history.

Empty stdout with exit code zero permits the operation. An explicit
`hookSpecificOutput.permissionDecision` of `allow` also permits it unless a
top-level `decision` is present. A deny decision, any other nonempty output,
invalid JSON or UTF-8 on stdout, execution failure, nonzero exit, or an eight-second timeout
denies the operation. Stdout and stderr are spooled to temporary files; more than
65,536 bytes in either stream denies the operation and is never parsed.

Configured checks run inside the same ACTIVE entry lock used by registered
mission entry. Chef reloads the effective adapter under that lock and rejects a
changed adapter. Registered entry receipts are revalidated inside the lock.
Concurrent transitions fail closed if the lock is busy. The checker must not
invoke another mission transition or acquire the ACTIVE entry lock.

The setting applies to Claude, Codex, and Grok through the common mission guard;
runtime overlays can select a runtime-specific checker. With no setting, existing
coordinator marker behavior is preserved. Subagents cannot execute lifecycle
markers. This is a cooperative policy hook, not a filesystem sandbox; trusted
installation and protected policy inputs remain necessary. Other independent
hooks that can reject a transition must be represented by the configured checker
to guarantee their decision precedes state mutation.
