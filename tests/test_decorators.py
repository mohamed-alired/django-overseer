import pytest
from django.core.checks import run_checks
from django.db import connection
from django.tasks import Task

import overseer
from overseer import registry
from overseer.decorators import OverseerTask
from overseer.scheduling import registry as schedules
from tests import tasks

pytestmark = pytest.mark.django_db


def test_overseer_task_is_a_django_task():
    assert isinstance(tasks.flaky, Task) and isinstance(tasks.flaky, OverseerTask)
    assert tasks.flaky.name == "flaky" and tasks.flaky.module_path == "tests.tasks.flaky"
    assert tasks.unique_by_args.queue_name == "emails"
    assert isinstance(tasks.flaky.using(priority=5), OverseerTask)
    assert tasks.flaky.call(0) == "done"


def test_policy_registered():
    p = registry.get_policy("tests.tasks.flaky")
    assert p.retries == 2 and p.backoff == "constant" and p.backoff_base == 10 and p.declared
    assert registry.get_policy("tests.tasks.picky").retry_on == (ValueError,)
    assert registry.get_policy("tests.tasks.with_context").timeout == 30
    assert registry.get_policy("tests.tasks.plain").declared is False


def test_policy_defaults_follow_settings(settings):
    settings.OVERSEER_DEFAULT_RETRIES = 4
    assert registry.get_policy("tests.tasks.plain").retries == 4


def test_schedule_decorator_registers_spec():
    spec = schedules.all_specs()["tests.tasks.scheduled_every_5"]
    assert spec.cron == "*/5 * * * *" and spec.kwargs == {"x": 1} and spec.queue_name == ""
    hb = schedules.all_specs()["heartbeat"]
    assert hb.interval_seconds == 60 and hb.task_path == "tests.tasks.scheduled_interval"


def test_schedule_validation():
    with pytest.raises(ValueError):
        overseer.schedule()
    with pytest.raises(ValueError):
        overseer.schedule("* * * * *", every=5)
    with pytest.raises(TypeError):
        overseer.schedule("* * * * *")(lambda: None)
    with pytest.raises(ValueError, match="invalid cron expression"):
        overseer.schedule("bad cron")(tasks.plain)
    with pytest.raises(ValueError, match="unknown timezone"):
        overseer.schedule("* * * * *", timezone="Nope/Zone")(tasks.plain)
    with pytest.raises(ValueError, match="names a day"):
        overseer.schedule("0 0 31 2 *")(tasks.plain)


def test_lazy_exports():
    assert overseer.task is overseer.decorators.task
    with pytest.raises(AttributeError):
        overseer.nope  # noqa: B018


@pytest.mark.skipif(
    not connection.features.supports_partial_indexes,
    reason="Django and Overseer both warn about the unique constraint on this database",
)
def test_checks_clean_by_default():
    assert [c.id for c in run_checks()] == []


def test_checks_report_bad_settings(settings):
    settings.OVERSEER_DEFAULT_BACKOFF = "random"
    settings.OVERSEER_PERMISSION = "nodot"
    settings.OVERSEER_RETENTION_DAYS = 0
    assert {c.id for c in run_checks()} >= {"overseer.E001", "overseer.E002", "overseer.E003"}


def test_check_warns_when_backend_cannot_defer(settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    assert "overseer.W001" in [c.id for c in run_checks()]
