"""Compatibility entry point; implementation: chef.guards.integrity."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.guards.integrity', runtime=None)
