"""Spawn real ``manage.py``-style processes (workers, the scheduler) against the test DB."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.db import connection

ROOT = Path(__file__).resolve().parent.parent


def db_is_shareable() -> bool:
    """A subprocess can only reach a file SQLite database or a real server."""
    db = settings.DATABASES["default"]
    if "sqlite" not in db["ENGINE"]:
        return True
    name = str(db.get("TEST", {}).get("NAME") or db["NAME"])
    return "memory" not in name and name != ":memory:"


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")] if p
    )
    env["DJANGO_SETTINGS_MODULE"] = settings.SETTINGS_MODULE
    env["OVERSEER_DB_NAME"] = str(connection.settings_dict["NAME"])
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Process:
    """A management command running in its own interpreter."""

    def __init__(self, command: str, *args: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "django", command, *args],
            cwd=ROOT,
            env=subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.output = ""

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, sig=signal.SIGINT, timeout: float = 15) -> int:
        if self.alive():
            self.proc.send_signal(sig)
        try:
            self.output, _ = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.output, _ = self.proc.communicate()
            raise AssertionError(f"process did not exit after {sig!r}:\n{self.output}") from None
        return self.proc.returncode

    def wait(self, timeout: float = 30) -> int:
        """Wait for the process to exit on its own."""
        try:
            self.output, _ = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.output, _ = self.proc.communicate()
            raise AssertionError(
                f"process still running after {timeout}s:\n{self.output}"
            ) from None
        return self.proc.returncode

    def kill(self) -> int:
        return self.stop(sig=signal.SIGKILL)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.alive():
            self.kill()


def wait_for(predicate, *, timeout: float = 20, interval: float = 0.1, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")
