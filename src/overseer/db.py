"""Database routing helpers: Overseer's tables may live on a non-default alias."""

from __future__ import annotations

from django.db import connections, router, transaction


def alias_for(model) -> str:
    """The database alias that writes to ``model`` go to, per the project's routers."""
    return router.db_for_write(model)


def atomic_for(model):
    """``transaction.atomic`` on the database that holds ``model``."""
    return transaction.atomic(using=alias_for(model))


def features_for(model):
    return connections[alias_for(model)].features
