"""Facade: the implementation lives in gateway.config.fingerprint."""

from gateway.config.fingerprint import *  # noqa: F401,F403
from gateway.config import fingerprint as _impl

# Re-export every public name so  keeps working unchanged.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_impl, _name))
del _name
