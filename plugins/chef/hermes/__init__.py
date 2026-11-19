"""Native Hermes plugin registration shim."""

import sys
from pathlib import Path

_package_root = str(Path(__file__).resolve().parents[1])
if _package_root not in sys.path:
    sys.path.insert(0, _package_root)

from chef.runtimes.hermes.plugin import register
