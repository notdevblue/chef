"""Compatibility entry point; implementation: chef.cli.unified."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.unified', runtime=None)
