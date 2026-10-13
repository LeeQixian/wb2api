# common and data

## common

Small shared helpers used across packages.

- `log.py` - one process-wide log sink; `set_logger()` installs a custom one,
  otherwise lines go to stdout/stderr. Every module logs through here.
- `paths.py` - filesystem roots resolved at import (`ACCOUNTS_DIR`,
  `USAGE_DIR`), overridden by `server.main()` through the http/upstream state.
- `ids.py` - `new_id(prefix)` for translated protocol objects.

## data

Pure data, no logic.

- `catalog_intl.json` - the international model catalogue snapshot.
- `catalog_cn.json` - the domestic model catalogue snapshot.

These are the bundled fallback for the model list. A live fetch from the
upstream `/v3/config` is layered on top by `upstream/catalog.py`; the snapshot
is what a machine with no live source still sees. `gateway/catalog.py` loads
them at import.
