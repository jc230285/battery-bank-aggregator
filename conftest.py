"""Pytest configuration for the battery-bank-aggregator test suite."""
import asyncio
import socket as _socket_module
import sys

import pytest

# Save the real socket.socket at conftest import time — before the HA plugin
# calls disable_socket() in its pytest_runtest_setup hook.  We restore it
# temporarily when creating asyncio event loops so their self-pipe init
# doesn't hit pytest-socket's SocketBlockedError.
_real_socket = _socket_module.socket


@pytest.fixture
def event_loop():
    """Provide a SelectorEventLoop without triggering pytest-socket.

    Temporarily restores the real socket.socket so asyncio's self-pipe
    can initialise, then puts back whatever pytest-socket had in place.
    All our tests are synchronous; this fixture only satisfies autouse
    dependencies injected by the globally-installed HA plugin."""
    _patched = _socket_module.socket
    _socket_module.socket = _real_socket
    try:
        if sys.platform == "win32":
            loop = asyncio.SelectorEventLoop()
        else:
            loop = asyncio.new_event_loop()
    finally:
        _socket_module.socket = _patched
    yield loop
    loop.close()


# Stub out HA autouse fixtures that have no meaning in this project but would
# otherwise pull in asyncio-dependent infrastructure for every test.
@pytest.fixture(autouse=True, scope="module")
def garbage_collection():
    yield


@pytest.fixture(autouse=True)
def expected_lingering_tasks():
    yield


@pytest.fixture(autouse=True)
def expected_lingering_timers():
    yield


@pytest.fixture(autouse=True)
def mock_get_source_ip():
    yield


@pytest.fixture(autouse=True)
def mock_network():
    yield


@pytest.fixture(autouse=True)
def wait_for_stop_scripts_after_shutdown():
    yield


@pytest.fixture(autouse=True)
def skip_stop_scripts():
    yield


@pytest.fixture(autouse=True)
def fail_on_log_exception():
    yield


@pytest.fixture(autouse=True)
def bcrypt_cost():
    yield
