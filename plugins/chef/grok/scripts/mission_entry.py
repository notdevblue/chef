"""Compatibility entry point; implementation: chef.cli.mission_entry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.mission_entry', runtime='grok')
