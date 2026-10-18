#!/usr/bin/env python3
"""wb2api - minimal local WorkBuddy -> OpenAI-compatible gateway (stdlib only).
Routes: /health /realm /v1/models /credits /v1/chat/completions /v1/responses."""
import os
import sys
import types

if __package__ in (None, ""):  # allow `python3 gateway/server.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import settings, translate, upstream  # noqa: F401
from gateway.common.log import log, set_logger  # noqa: F401
from gateway.http import state as _state  # noqa: F401
from gateway.http.errors import BadJSON, BodyTooLarge  # noqa: F401
from gateway.http.handler import Handler  # noqa: F401
from gateway.http.runner import GatewayServer, build_server  # noqa: F401
from gateway.http.usage import _usage_fields, record_row  # noqa: F401
from gateway.main import _parse_args, main, refresh_pool  # noqa: F401
from gateway.http.state import (  # noqa: F401
    ACCOUNTS_DIR, ARGS, CHAT_SLOT_WAIT_SECONDS, MAX_CONCURRENT_CHAT,
    MAX_PAYLOAD_BYTES, POOL, POOL_REFRESH_SECONDS, USAGE_DIR, _chat_slots,
    _pool_lock,
)

#: import-time defaults kept for callers that read them directly.
DEFAULT_ACCOUNTS_DIR = _state._DEFAULT_ACCOUNTS_DIR
DEFAULT_USAGE_DIR = _state._DEFAULT_USAGE_DIR


class _FacadeModule(types.ModuleType):
    """Forward state writes so `server.POOL = ...` reaches the http state."""

    _FORWARD = ("ACCOUNTS_DIR", "ARGS", "POOL", "USAGE_DIR", "_chat_slots",
                "_pool_lock", "MAX_PAYLOAD_BYTES", "MAX_CONCURRENT_CHAT")

    def __getattr__(self, name):
        if name in self._FORWARD:
            return getattr(_state, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self._FORWARD:
            setattr(_state, name, value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule

if __name__ == "__main__":
    sys.exit(main())
