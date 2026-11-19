"""Compatibility entry point; implementation: chef.runtimes.grok.injection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.runtimes.grok.injection', runtime=None)
