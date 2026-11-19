"""Compatibility entry point; implementation: chef.cli.runtime."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.runtime', runtime=None)
