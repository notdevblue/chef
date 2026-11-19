"""Compatibility entry point; implementation: chef.cli.distill."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.distill', runtime='codex')
