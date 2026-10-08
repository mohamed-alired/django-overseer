from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        from . import tasks  # noqa: F401 - registers the tasks and their schedules
