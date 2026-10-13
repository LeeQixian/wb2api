# cli

Command-line management, replacing the upstream dashboard.

`python3 -m gateway.wbctl` (facade in `gateway/wbctl.py`) dispatches to
`cli/wbctl.py`.

Commands: `login`, `import`, `list`, `remove`, `enable`, `disable`, `refresh`,
`credits`, `checkin`, `daily-chat`, `growthplan`, `realm`, `product`,
`set-switch`, `set-prompt`, `sync-nicknames`, `set-key`, `clear-key`.

## Design notes

- **Account references.** Commands taking `<uid>` accept a full uid, a unique
  uid prefix, or a unique nickname (`AccountPool.resolve`). Ambiguity errors
  out; it never picks one silently.
- **`growthplan` is separate from `--tasks`.** One-shot growth tasks are not
  run by the scheduler; this command runs them on demand.
- Exit code is non-zero with a one-line stderr message on failure.
