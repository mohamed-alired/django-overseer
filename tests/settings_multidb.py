"""Overseer's tables (and the task backend's) on a second database alias through a router:
``pytest --ds=tests.settings_multidb tests/test_multidb.py``."""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "ops": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}
DATABASE_ROUTERS = ["tests.settings_multidb.OpsRouter"]


class OpsRouter:
    apps = {"overseer", "django_tasks_db"}

    def db_for_read(self, model, **hints):
        return "ops" if model._meta.app_label in self.apps else None

    db_for_write = db_for_read

    def allow_migrate(self, db, app_label, **hints):
        return (db == "ops") == (app_label in self.apps)
