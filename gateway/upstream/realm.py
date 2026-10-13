"""Global exit state: read, switch and persist."""

import json
import os
import time
from gateway import settings as wb_settings
from gateway.common.log import log
from gateway.common import paths
from gateway import translate as tr


def realm_state_file(accounts_dir):
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    source built the constant from the default ACCOUNTS_DIR at import time,
    so a later --accounts-dir still read and wrote the switch next to the
    script, and the reported exit did not match the configured account store.
    """
    return os.path.join(accounts_dir, "active_realm.json")


def get_realm():
    return tr.CURRENT_REALM


def set_realm(realm):
    """Single source of truth lives in translate.CURRENT_REALM (read by
    model_default_effort & friends); mirror it here for direct importers."""
    if realm in ("intl", "cn"):
        tr.CURRENT_REALM = realm
    return tr.CURRENT_REALM


def load_persisted_realm(accounts_dir):
    path = realm_state_file(accounts_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            r = d.get("realm")
            if r in ("intl", "cn"):
                return set_realm(r)
        except Exception as e:
            log("could not load active realm: %s" % e)
    return get_realm()


def save_persisted_realm(accounts_dir, realm):
    if realm in ("intl", "cn"):
        set_realm(realm)
        try:
            os.makedirs(accounts_dir, exist_ok=True)
            with open(realm_state_file(accounts_dir), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(),
                           "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")},
                          fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return get_realm()
