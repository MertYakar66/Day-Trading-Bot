"""Shared pytest fixtures for the intraday engine test suite.

All fixtures are deterministic and network-free (synthetic provider only).
"""

from __future__ import annotations

import socket
from datetime import date

import pytest

from intraday.config import EngineConfig
from intraday.data.store import ParquetStore
from intraday.data.synthetic import SyntheticDataProvider

# A representative trading day (Monday, 2026-05-18) used across tests.
TEST_DAY = date(2026, 5, 18)

_NET_BLOCKED = (
    "network is blocked in the test suite (this paper-only engine never connects to "
    "Theta/IBKR/Yahoo or any host); mark a test @pytest.mark.allow_network to opt out"
)


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    """Enforce the headline safety guarantee MECHANICALLY rather than by convention:
    the engine opens no network socket during tests. Any real outbound connection
    attempt raises, so a future test that accidentally reaches a live data source
    fails loudly instead of silently passing. Opt out with @pytest.mark.allow_network
    (no test currently needs it). Patches connection-time only, so local file/parquet
    I/O is untouched and import-time code is unaffected (the fixture runs per test)."""
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(_NET_BLOCKED)

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked, raising=False)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)


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
