import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client

from overseer.models import Job, Run

PASSWORD = "pw-123456"


@pytest.fixture(autouse=True)
def _reset_calls():
    from tests import tasks

    tasks.CALLS.clear()
    yield
    tasks.CALLS.clear()


def work(**kwargs):
    """Run the reference database worker until the queue is drained."""
    call_command(
        "db_worker",
        queue_name="*",
        batch=True,
        startup_delay=False,
        interval=0.0,
        verbosity=0,
        **kwargs,
    )


@pytest.fixture
def worker(transactional_db):
    """The reference worker closes DB connections, which only works outside a test transaction."""
    return work


@pytest.fixture
def staff_user(db):
    user = get_user_model().objects.create_user("ops", "ops@example.com", PASSWORD, is_staff=True)
    from django.contrib.auth.models import Permission

    user.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["view_dashboard", "manage_jobs", "manage_schedules"]
        )
    )
    return user


@pytest.fixture
def staff_client(staff_user):
    c = Client()
    c.force_login(staff_user)
    return c


@pytest.fixture
def job_of():
    def _job_of(result):
        return Job.objects.get(runs__result_id=str(result.id))

    return _job_of


@pytest.fixture
def run_of():
    def _run_of(result):
        return Run.objects.get(result_id=str(result.id))

    return _run_of
