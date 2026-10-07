from django.apps import AppConfig


class OverseerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "overseer"
    verbose_name = "Overseer"

    def ready(self):
        from . import checks, recorders  # noqa: F401  (registers checks)

        recorders.connect()
