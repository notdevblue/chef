"""Compatibility for existing script paths and Python importers."""

import sys
from importlib import import_module


def expose(namespace, module_name, runtime=None):
    if sys.version_info < (3, 11):
        raise SystemExit("Chef requires Python 3.11 or newer. In Hermes, use the absolute Chef CLI Python executable provided in the native context.")
    name = namespace["__name__"]
    if name == "__main__":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    module = import_module(module_name)
    if name == "__main__":
        main = getattr(module, "main", None)
        if main is not None:
            raise SystemExit(main(runtime=runtime) if runtime is not None else main())
        return
    namespace.update(vars(module))
    sys.modules[name] = module
