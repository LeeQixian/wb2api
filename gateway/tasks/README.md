# tasks

The credit-earning task engine.

## Design

Two entry points share one surface:

- **Daily tasks** run from the background scheduler (`--tasks`): token
  keep-alive, CN check-in, cat travel and the intl daily activity chat.
- **Growth tasks** are one-shot (accept, light up, claim) and run only through
  `wbctl growthplan <uid>`; the scheduler never touches them.

- `base.py` - task specs and ids, the desktop-only list, pacing.
- `growth.py` - fetch / accept / claim / report; `run_growth_tasks`,
  `run_night_growth`.
- `travel.py` - cat travel status and departure, plus the missing-Buddy
  self-heal.
- `scheduler.py` - the background loop.

`gateway/scheduler.py` at the top level is the import surface for the
scheduler.

## Invariants

- **`enable` / `disable` gates inference only.** The scheduler keeps every
  runnable account alive (keep-alive, check-in, travel) regardless of its
  enabled state; enable controls whether an account serves requests and spends
  credits, not whether it is maintained.
- **`first_buddy` runs first.** Its reward is the Buddy instance, and it gates
  the single tasks and travel, so it is completed and claimed before the rest.
- **`accept_tasks` reports accepted/failed per task**, not a bare success. A
  silently failed accept makes every later report land on an unaccepted task
  and progress stays at zero.
- **Pacing is randomized.** 2-7s between actions within an account and 3-10s
  between accounts; the schedule offsets move by minutes, not hours. A fixed
  cadence is itself a signature.
- **Enterprise accounts are skipped silently** (no personal growth system).

## Config

- `--tasks` enables the scheduler.
- `schedule.checkin/travel/keepalive/cat` - the firing hours, read from
  `accounts/settings.json`; the scheduler re-reads them every tick (no restart)
  and per-row invalid values fall back to the built-in grid.
- `daily_chat_web` selects the intl daily-activity channel.
- Env: `WB_WEB_TURN_TIMEOUT`.
