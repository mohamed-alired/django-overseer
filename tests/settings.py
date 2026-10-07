"""Settings for the django-overseer test suite."""

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

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
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
        "ENQUEUE_ON_COMMIT": False,
    },
    "immediate": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"},
    "dummy": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"},
}
