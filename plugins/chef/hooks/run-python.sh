#!/bin/sh
# Select the interpreter before starting a hook so its exit code reaches the host.
for interpreter in python3 python; do
    if command -v "$interpreter" >/dev/null 2>&1 &&
        "$interpreter" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        exec "$interpreter" "$@"
    fi
done
printf '%s\n' 'Chef hooks require Python 3.11 or newer (python3 or python on PATH).' >&2
exit 2
