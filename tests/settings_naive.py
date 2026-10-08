"""The suite's time-sensitive parts in a project with ``USE_TZ = False``:
``pytest --ds=tests.settings_naive tests/test_naive_time.py``."""

from .settings import *  # noqa: F401,F403

USE_TZ = False
TIME_ZONE = "Europe/Paris"
