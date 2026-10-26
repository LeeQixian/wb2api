"""Facade: the implementation lives in gateway.config.prompt."""

from gateway.config.prompt import *  # noqa: F401,F403
from gateway.config import prompt as _impl

# Mirror every public name of the implementation module.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_impl, _name))
del _name
