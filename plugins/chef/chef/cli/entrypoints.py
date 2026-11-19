"""Compatibility for existing script paths and Python importers."""

import sys
from importlib import import_module


def expose(namespace, module_name, runtime=None):
    name = namespace["__name__"]
    module = import_module(module_name)
    if name == "__main__":
        main = getattr(module, "main", None)
        if main is not None:
            raise SystemExit(main(runtime=runtime) if runtime is not None else main())
        return
    namespace.update(vars(module))
    sys.modules[name] = module
