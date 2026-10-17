"""Shared test configuration.

``tools/`` is not an installed package: the sample generator and the PDF/DOCX
writers are repository tooling, invoked by path. Adding it to ``sys.path`` here
lets the tests import the very same writer the corpus was built with, so a test
cannot pass against a stub that the real samples never exercise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
SAMPLES = REPO_ROOT / "data" / "samples"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    return SAMPLES
