"""APScheduler wiring: heartbeat, watcher, and the consolidation sweep. Started by `nwb serve --scheduler`."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from nwb.container import Container
from nwb.obs.log import get_logger

log = get_logger(__name__)


def build_scheduler(c: Container) -> BackgroundScheduler:
    tz = ZoneInfo(c.agent_config.timezone)
    sched = BackgroundScheduler(timezone=tz)
    hb = c.heartbeat_config
    if hb.enabled:
        sched.add_job(
            lambda: c.heartbeat.tick(datetime.now(tz)),
            IntervalTrigger(minutes=hb.interval_minutes, timezone=tz),
            id="heartbeat",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    wc = c.actions_config.watcher
    if wc.enabled:
        sched.add_job(
            lambda: c.watcher.run(datetime.now(tz).date()),
            IntervalTrigger(minutes=wc.interval_minutes, timezone=tz),
            id="watcher",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    sched.add_job(
        c.consolidation.sweep,
        IntervalTrigger(minutes=c.memory_config.consolidation.sweep_minutes, timezone=tz),
        id="consolidate_sweep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return sched
