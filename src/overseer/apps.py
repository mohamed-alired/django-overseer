from django.apps import AppConfig


class OverseerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "overseer"
    verbose_name = "Overseer"

    def ready(self):
        from django.utils.module_loading import autodiscover_modules, import_string

        from . import checks, conf, recorders  # noqa: F401  (registers checks)

        recorders.connect()
        # Load task modules so ``overseer.task`` policies and ``overseer.schedule``
        # declarations exist in every process (web, worker, scheduler), not only in the
        # one that happened to import them.
        if conf.get_setting("OVERSEER_AUTODISCOVER"):
            autodiscover_modules("tasks")
        for dotted in conf.get_setting("OVERSEER_TASK_MODULES"):
            import_string(dotted) if ":" in dotted else __import__(dotted)
