# common

Small shared helpers used across packages.

- `log.py` - one process-wide log sink; `set_logger()` installs a custom one,
  otherwise lines go to stdout/stderr. Every module logs through here.
- `paths.py` - filesystem roots resolved at import (`ACCOUNTS_DIR`,
  `USAGE_DIR`), overridden by `server.main()` through the http/upstream state.
- `ids.py` - `new_id(prefix)` for translated protocol objects.
