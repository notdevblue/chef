"""Compatibility entry point; implementation: chef.workers.cli."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.workers.cli', runtime=None)
