"""Windows Computer Use V1 public API."""

import platform as _platform


_IS_WINDOWS = _platform.system() == "Windows"
__all__ = []

if _IS_WINDOWS:
    from . import _windows as _impl

    _instrumentation = _impl.public._instrumentation

    from importlib import import_module as _import_module

    _public_api = _import_module(_impl.__name__ + ".public.api")

    __all__ = list(_public_api.__all__)
    for _name in __all__:
        globals()[_name] = getattr(_public_api, _name)

    def __getattr__(name):
        return getattr(_public_api, name)
