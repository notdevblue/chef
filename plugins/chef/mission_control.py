"""Compatibility entry point; implementation: chef.mission.transitions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.mission.transitions', runtime=None)
