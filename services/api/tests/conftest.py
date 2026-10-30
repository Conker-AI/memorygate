"""Every test here runs under the repository-write guard (see repository_guard.py)."""

from repository_guard import (  # noqa: F401  (pytest finds hooks by name in this module)
    pytest_configure,
    pytest_runtest_makereport,
    pytest_runtest_protocol,
)
