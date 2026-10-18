"""Task engine facade: exposes the growth/travel surface as gateway.tasks.X."""
import sys
import types

from gateway.common.log import log, set_logger  # noqa: F401
from gateway.tasks.base import (  # noqa: F401
    BILL_BASE, CHAT_BASE, DESKTOP_ONLY_TASKS, DESKTOP_UA, EXPERT_ID_POOL,
    NIGHT_TASK_CODES, TASK_SPECS, TEAM_ID_POOL, WEB_BASE, WEB_UA, _pause,
    in_night_window,
)
from gateway.tasks.growth import (  # noqa: F401
    accept_tasks, build_event, claim_task, fetch_growth_summary,
    fetch_growth_tasks, report_events, run_growth_tasks, run_night_growth,
)
from gateway.tasks.travel import (  # noqa: F401
    _travel_depart, _travel_status, do_cat_travel, ensure_buddy,
)
from gateway import accounts as _accounts  # noqa: F401
from gateway.tasks import growth as _growth
from gateway.tasks import travel as _travel


class _FacadeModule(types.ModuleType):
    """Forward `tasks._accounts = x` patches to the submodules."""

    _FORWARD = ("fetch_growth_tasks", "fetch_growth_summary", "accept_tasks",
                "claim_task", "build_event", "report_events", "_progress_one",
                "run_growth_tasks", "run_night_growth", "do_cat_travel",
                "ensure_buddy", "_travel_status", "_travel_depart", "_pause")

    def __setattr__(self, name, value):
        if name == "_accounts":
            _growth._accounts = value
            _travel._accounts = value
        elif name in self._FORWARD:
            setattr(_growth, name, value)
            setattr(_travel, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
