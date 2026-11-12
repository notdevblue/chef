import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import subagent_allow


if __name__ == "__main__":
    sys.exit(subagent_allow.main(runtime="codex"))
