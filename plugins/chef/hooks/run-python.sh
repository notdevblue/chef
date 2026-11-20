#!/bin/sh
# In hooks.json, sh treats the # after the quoted set argument as part of that
# argument and runs exec. PowerShell treats it as a comment and runs line two.
# cmd uses the two commands after & on line one. Its Python bootstrap reads the
# root from the environment because cmd /C receives escaped double quotes.
# The POSIX :? expansion delays Grok's root substitution until sh runs.

# Windows Git Bash uses the same interpreter as the Windows native shells.
case "${OSTYPE-}" in
    msys*|cygwin*) exec python "$@" ;;
esac

# Select the interpreter before starting a hook so its exit code reaches the host.
for interpreter in python3 python; do
    if command -v "$interpreter" >/dev/null 2>&1 &&
        "$interpreter" -c 'import sys; sys.exit(sys.version_info < (3, 11))' </dev/null >/dev/null 2>&1; then
        exec "$interpreter" "$@"
    fi
done
printf '%s\n' 'Chef hooks require Python 3.11 or newer (python3 or python on PATH).' >&2
exit 2
