"""Compatibility entry point; implementation: chef.questions.records."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from chef.cli.entrypoints import expose

expose(globals(), 'chef.questions.records', runtime=None)
