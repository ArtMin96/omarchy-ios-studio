"""Stub pymobiledevice3 for importing the real device feature modules where pymobiledevice3 isn't installed.

    with stub_pmd3():
        from iosstudio.device import kit
        kit.load_features(registry, "real")

Every `pymobiledevice3.*` module exists and every name in it is a fresh class (an Exception subclass, so
`except X` clauses work). With the real pymobiledevice3 installed (the daemon's venv) nothing is stubbed.
Modules imported inside the block (stubs and the feature modules that saw them) are dropped afterwards.
"""
from __future__ import annotations

import contextlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (Exception,), {})
        setattr(self, name, cls)
        return cls


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name == "pymobiledevice3" or name.startswith("pymobiledevice3."):
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        m = _StubModule(spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        pass


def have_pmd3() -> bool:
    return importlib.util.find_spec("pymobiledevice3") is not None


@contextlib.contextmanager
def stub_pmd3(prefixes: tuple[str, ...] = ("pymobiledevice3", "iosstudio.device.features")):
    before = set(sys.modules)
    finder = _StubFinder()
    if not have_pmd3():
        sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
            for k in [k for k in sys.modules if k not in before and k.startswith(prefixes)]:
                del sys.modules[k]
