"""Compatibility entry point; implementation: chef.slots.commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.slots.commands', runtime='codex')
