"""Same suite on MariaDB / MySQL: ``pytest --ds=tests.settings_mysql``."""

import os

from .settings import *  # noqa: F401,F403

_name = os.environ.get("MYSQL_DATABASE", "overseer")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": _name,
        "USER": os.environ.get("MYSQL_USER", "root"),
        "PASSWORD": os.environ.get("MYSQL_PASSWORD", "overseer"),
        "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "PORT": os.environ.get("MYSQL_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
        "TEST": {"NAME": f"{_name}_test", "CHARSET": "utf8mb4"},
    }
}
if os.environ.get("OVERSEER_DB_NAME"):
    DATABASES["default"]["NAME"] = os.environ["OVERSEER_DB_NAME"]
