"""Compatibility entry point; implementation: chef.guards.mission."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.guards.mission', runtime=None)
