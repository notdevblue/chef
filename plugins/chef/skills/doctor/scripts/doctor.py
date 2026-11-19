"""Compatibility entry point; implementation: chef.cli.doctor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.doctor', runtime=None)
