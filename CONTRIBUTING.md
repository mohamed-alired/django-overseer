# Contributing

```bash
git clone https://github.com/mohamed-alired/django-overseer.git
cd django-overseer
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

- `pytest` runs the unit, dashboard, end-to-end and performance tests on an in-memory SQLite
  database. The integration tests that spawn real worker and scheduler processes need a
  shareable database: `OVERSEER_SQLITE_FILE=/tmp/overseer.sqlite3 pytest
  tests/test_integration.py`, or PostgreSQL (`pytest --ds=tests.settings_postgres`, with
  `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`), which also runs the concurrency tests.
- `ruff check src tests && ruff format src tests` before committing.
- Model changes need a migration: `DJANGO_SETTINGS_MODULE=tests.settings PYTHONPATH=src:.
  python -m django makemigrations overseer`. CI fails when one is missing.
- Add a changelog entry under `## [Unreleased]`.

Releases are automatic: bump `__version__` in `src/overseer/__init__.py`, move the
changelog entry under the new version, merge to `main`; when CI passes, the release
workflow publishes to PyPI and creates the GitHub release.
