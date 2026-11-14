from urllib.request import urlopen

import pytest


def test_local_server_round_trip(local_http_server):
    with urlopen(local_http_server, timeout=2) as response:
        assert response.read() == b"loopback-ok"


def test_external_dependency_is_blocked(external_service):
    pytest.fail("external_service fixture should fail before the test body")


@pytest.mark.asyncio
async def test_async_control():
    assert (await _async_value()) == 42


async def _async_value():
    return 42


def test_parametrized_fixture(parametrized_resource):
    assert parametrized_resource in {"alpha", "beta"}
