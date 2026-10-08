"""Settings for the django-overseer test suite."""

import os

SECRET_KEY = "not-a-secret"
DEBUG = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "django.contrib.staticfiles",
    "django_tasks_db",
    "overseer",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

# ``OVERSEER_SQLITE_FILE`` switches to a file database so the integration tests can share
# it with real worker and scheduler subprocesses; ``OVERSEER_DB_NAME`` is what those
# subprocesses receive (the *test* database name of the parent pytest process).
_sqlite_file = os.environ.get("OVERSEER_SQLITE_FILE")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _sqlite_file or ":memory:",
        "TEST": {"NAME": f"{_sqlite_file}.test" if _sqlite_file else None},
        # WAL lets the worker, scheduler and test process read and write concurrently, and
        # IMMEDIATE transactions take the write lock up front so a writer waits (busy
        # timeout) instead of failing with "database is locked" after its first read.
        "OPTIONS": {
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL;",
        }
        if _sqlite_file
        else {},
    }
}
if os.environ.get("OVERSEER_DB_NAME"):
    DATABASES["default"]["NAME"] = os.environ["OVERSEER_DB_NAME"]
ROOT_URLCONF = "tests.urls"
STATIC_URL = "/static/"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# Django 6.1+ mailer registry (ignored by 6.0); keeps the deprecation warnings out of the suite.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.backend.DatabaseBackend",
        "QUEUES": ["default", "emails", "reports"],
    },
    "immediate": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"},
    "dummy": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"},
}

OVERSEER_TASK_MODULES = ["tests.tasks"]
