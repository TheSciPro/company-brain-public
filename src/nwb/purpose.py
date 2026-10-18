"""Why a run exists. Config and the agent both read this; neither package owns it."""

from __future__ import annotations

from typing import Literal, get_args

Purpose = Literal["chat", "heartbeat", "cron", "consolidate", "eval"]
# Every purpose a run can take. Onboarding uses a subset, chosen in config, not a branch per job.
PURPOSES: frozenset[str] = frozenset(get_args(Purpose))
