"""Upstream facade: exposes the outbound-request surface as gateway.upstream.X.
Implementation lives in the sibling modules; live state is re-exported."""
import sys
import types

from gateway.common.log import log, set_logger  # noqa: F401
from gateway.upstream import realm as _realm  # noqa: F401
from gateway.upstream.catalog import (  # noqa: F401
    CHAT_PATH, MODELS_PATH, REMOTE_CONFIG_PATH, UPSTREAM, curate_remote_catalog,
    curated_live_sources, fetch_endpoint_models, fetch_models, fetch_models_all,
    fetch_remote_product_config, merge_catalog, merge_reasoning, model_entry,
    parse_remote_catalog, snapshot_credits,
)
from gateway.upstream.client import AFFINITY_DEBUG, open_upstream  # noqa: F401
from gateway.upstream.state import (  # noqa: F401
    _POOL, ACCOUNTS_DIR, USAGE_DIR, _lock,
)
from gateway.upstream.errors import ContentRejected, RateLimited  # noqa: F401
from gateway.upstream.retry import LocalNetworkError  # noqa: F401
from gateway.upstream.guards import (  # noqa: F401
    _daily_usage, apply_daily_token_limit, auto_switch_product_enabled,
    daily_tokens_by_account, usage_log_path,
)
from gateway.upstream.realm import (  # noqa: F401
    get_realm, load_persisted_realm, realm_state_file, save_persisted_realm,
    set_realm,
)
from gateway.upstream.retry import (  # noqa: F401
    MAX_PRODUCT_SWITCHES, is_transient, parse_rate_limit_reset,
    rate_limit_code, rate_limit_is_account_level, rate_limit_is_soft_shape,
    realm_model_throttled, reset_switch_counter, restriction_is_account_level,
    retry_after_seconds,
)
from gateway.upstream.timeouts import (  # noqa: F401
    _apply_stream_idle_timeout, upstream_timeouts,
)
from gateway import accounts as wb_accounts  # noqa: F401
from gateway.upstream.catalog import (  # noqa: F401
    _catalog_lock, curated_live_sources as _curated_live_sources,
)
from gateway.protocol.request import build_upstream_body  # noqa: F401
from gateway import protocol as _protocol  # noqa: F401
from gateway.protocol.model_info import (  # noqa: F401
    exclusive_realm, is_model_banned,
)
from gateway.protocol.request import banned_model_message  # noqa: F401


from gateway.upstream import catalog as _catalog
from gateway.upstream import client as _client
from gateway.upstream import state as _state


class _FacadeModule(types.ModuleType):
    """Keep module-level state writes pointing at the state module."""

    def __setattr__(self, name, value):
        if name in ("_POOL", "USAGE_DIR", "ACCOUNTS_DIR", "_lock"):
            setattr(_state, name, value)
        elif name == "wb_accounts":
            _catalog.wb_accounts = value
            _client.wb_accounts = value
            super().__setattr__(name, value)
            return
        elif name in ("fetch_models", "fetch_endpoint_models", "model_entry",
                      "merge_catalog", "snapshot_credits"):
            setattr(_catalog, name, value)
            super().__setattr__(name, value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
