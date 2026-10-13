# tasks

The credit-earning task engine (off by default).

## Design

Two entry points, one shared surface:

- **Daily tasks** run from the background scheduler (`--tasks`) for every
  account that can execute them, regardless of `enabled`: enable/disable only
  controls whether an account serves inference and spends credits, not whether
  it is kept alive.
- **Growth tasks** are one-shot and are NOT run by the scheduler. They run only
  through `wbctl growthplan <uid>`.

- `base.py` - task specs, ids, the desktop-only list, `_pause`.
- `growth.py` - fetch / accept / claim / report; `run_growth_tasks`,
  `run_night_growth`.
- `travel.py` - cat travel status / depart, and the missing-Buddy self-heal.
- `scheduler.py` - the background loop (see its module docstring).

`gateway/scheduler.py` is a facade over `tasks/scheduler.py`.

## What each piece fixes

- **Prerequisite chain.** `first_buddy` is an auto task whose reward is the
  Buddy instance, and it gates the single tasks and travel; it is completed and
  claimed first.
- **Accept reporting.** `accept_tasks` returns accepted/failed lists rather
  than a bare bool, because a silently failed accept makes every later report
  land on an unaccepted task and progress stays at zero.
- **Anti-risk-control timing.** Randomized pacing (2-7s within an account,
  3-10s between accounts) and per-day schedule offsets keep the timing off a
  machine beat. Do not replace them with constants.
- **Enterprise skip.** Enterprise accounts have no growth system; tasks are
  skipped silently to avoid a 403.
- **Cat travel self-heal.** Travel checks for the Buddy and completes
  `first_buddy` itself when the instance is missing.

## Unresolved

- `run_growth_tasks` and `run_night_growth` share task-spec knowledge with
  `base.py`; a spec change must be reflected in both paths.

## Config fields

- `--tasks` enables the scheduler.
- `daily_chat_web` selects the intl daily-activity channel.
- Env: `WB_WEB_TURN_TIMEOUT`.
