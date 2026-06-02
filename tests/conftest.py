"""Shared pytest fixtures for the intraday engine test suite.

All fixtures are deterministic and network-free (synthetic provider only).
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.config import EngineConfig
from intraday.data.store import ParquetStore
from intraday.data.synthetic import SyntheticDataProvider

# A representative trading day (Monday, 2026-05-18) used across tests.
TEST_DAY = date(2026, 5, 18)


@pytest.fixture
def config() -> EngineConfig:
    return EngineConfig.default()


@pytest.fixture
def provider(config: EngineConfig) -> SyntheticDataProvider:
    return SyntheticDataProvider(config.data, config.session)


@pytest.fixture
def day() -> date:
    return TEST_DAY


@pytest.fixture
def spy_bars(provider, day):
    return provider.get_bars("SPY", day, "1m")


@pytest.fixture
def spy_chain(provider, day):
    return provider.get_option_chain("SPY", day)


@pytest.fixture
def spy_tape(provider, day):
    return provider.get_option_tape("SPY", day)


@pytest.fixture
def store(tmp_path) -> ParquetStore:
    return ParquetStore(root=tmp_path / "data_store")
