from __future__ import annotations

import pytest

from tests.factories import NOW, make_config


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def config():
    return make_config()
