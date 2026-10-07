"""Same suite on PostgreSQL: ``pytest --ds=tests.settings_postgres``."""

import os

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "overseer"),
        "USER": os.environ.get("PGUSER", "drf"),
        "PASSWORD": os.environ.get("PGPASSWORD", "drf"),
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
        "TEST": {"NAME": os.environ.get("PGDATABASE", "overseer") + "_test"},
    }
}
if os.environ.get("OVERSEER_DB_NAME"):
    DATABASES["default"]["NAME"] = os.environ["OVERSEER_DB_NAME"]
