"""The integration drill runs under the same repository-write guard as the API tests."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "services" / "api" / "tests"))

from repository_guard import (  # noqa: F401  (pytest finds hooks by name in this module)
    pytest_configure,
    pytest_runtest_makereport,
    pytest_runtest_protocol,
)
