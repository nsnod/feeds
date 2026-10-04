from __future__ import annotations

import pytest

from tests.factories import NOW, TEST_CONFIG_DIR, make_config


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    """Anything that loads "the default config" (e.g. the CLI) gets the pinned test copy,
    never the user's editable config/ folder."""
    monkeypatch.setenv("GEMBOT_CONFIG_DIR", str(TEST_CONFIG_DIR))


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def config():
    return make_config()
