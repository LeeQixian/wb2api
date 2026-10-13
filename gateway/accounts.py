"""Accounts facade.

Re-exports the account surface the rest of the gateway and the tests use as
gateway.accounts.X. Implementation lives in gateway.account.*
"""
import re  # noqa: F401
import sys
import types

from gateway import identity  # noqa: F401
from gateway import settings as wb_settings  # noqa: F401
from gateway.fingerprint import derive_id, generate_request_id  # noqa: F401

from gateway.account import jwt as _jwt_mod  # noqa: F401
from gateway.account.affinity import SessionAffinity, _human_delta  # noqa: F401
from gateway.account.backoff import (  # noqa: F401
    _exponential_backoff, breaker_backoff, degrade_backoff, next_local_4am,
    soft_backoff, unverified_soft_backoff,
)
from gateway.account.credentials import (  # noqa: F401
    credential_to_kwargs, deduction_end_text, fetch_account_profile,
    web_origin_for,
)
from gateway.account.credits import CreditsRefresher  # noqa: F401
from gateway.account.jwt import (  # noqa: F401
    AUTH_STATE_PATH, AUTH_TOKEN_PATH, CHECKIN_PATH, GET_RESOURCE_PATH,
    LOGIN_ACCOUNT_PATH, LOGIN_PENDING, LOGIN_TTL_SECONDS, REALM_CONFIGS,
    REFRESH_PATH, USER_AGENT, _jwt_claims, detect_realm_from_token,
    domain_for_realm, get_realm_config, jwt_exp, jwt_issuer, jwt_uid,
    normalize_epoch, realm_evidence,
)
from gateway.account.model import Account  # noqa: F401
from gateway.account.pool import AccountPool  # noqa: F401
from gateway.account import transport as _transport  # noqa: F401
from gateway.account.transport import (  # noqa: F401
    _retryable, http_json, opener_for_proxy, urlopen,
)


class _FacadeModule(types.ModuleType):
    """Forward patches of the transport helpers onto transport itself."""

    _FORWARD = ("urlopen", "http_json", "opener_for_proxy")

    def __setattr__(self, name, value):
        if name in self._FORWARD:
            setattr(_transport, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
