"""Facade: the implementation lives in gateway.config.fingerprint."""

from gateway.config.fingerprint import *  # noqa: F401,F403
from gateway.config import fingerprint as _impl

# Mirror every public name of the implementation module.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_impl, _name))
del _name
