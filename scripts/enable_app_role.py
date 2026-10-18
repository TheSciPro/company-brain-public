"""Turn on LOGIN for the nw_app role. Run as the database owner after ``nwb migrate``.

Password comes from ``NWB_APP_PASSWORD``. The owner URL is ``NWB_MIGRATE_URL`` (or ``NEON_DIRECT_URL``),
falling back to ``DATABASE_URL`` only when that URL is already the owner. This script never prints the URL
or the password.

    NWB_MIGRATE_URL=postgresql://owner@host/db NWB_APP_PASSWORD=... python scripts/enable_app_role.py
"""

from __future__ import annotations

import os
import sys

import psycopg

from nwb.config import get_settings
from nwb.db.app_role import APP_ROLE, enable_login
from nwb.obs.log import get_logger, setup_logging

log = get_logger(__name__)


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    password = os.environ.get("NWB_APP_PASSWORD", "")
    if not password:
        log.error("NWB_APP_PASSWORD is required")
        return 2
    if settings.db_backend != "postgres":
        log.error("app role login is only for Postgres")
        return 2
    url = settings.migrate_url or settings.pg_dsn
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            enable_login(conn, password)
    except psycopg.Error:
        log.error("could not enable app role login", role=APP_ROLE)
        return 1
    log.info("app role login enabled", role=APP_ROLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
