"""Compatibility entry point; implementation: chef.cli.p4c."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.cli.p4c', runtime='grok')
