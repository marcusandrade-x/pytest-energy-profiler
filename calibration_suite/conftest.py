"""Suíte de calibração: durações CONHECIDAS por fixture/escopo.

Serve para validar o profiler (atribuição setup/corpo/teardown, aninhamento sem
dupla contagem, contagem de instâncias e de usos). Não é um projeto real.
"""
import time

import pytest


def burn(seconds: float) -> None:
    """Ocupa a CPU por `seconds` (espera ativa, para que a energia acompanhe o tempo)."""
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x += 1


@pytest.fixture(scope="session")
def session_res():
    burn(1.0)          # setup  ~1000 ms
    yield "session"
    burn(0.4)          # teardown ~400 ms


@pytest.fixture(scope="module")
def module_res():
    burn(0.5)          # setup  ~500 ms (uma vez por módulo)
    yield "module"
    burn(0.2)          # teardown ~200 ms


@pytest.fixture
def func_res():
    burn(0.1)          # setup  ~100 ms (uma vez por teste que a usa)
    yield "function"
    burn(0.05)         # teardown ~50 ms


@pytest.fixture
def addfin(request):
    burn(0.1)
    request.addfinalizer(lambda: burn(0.05))   # finalizer via request.addfinalizer
    return "addfin"


@pytest.fixture
def cheap():
    return 1           # sem teardown: não deve gerar linha de teardown


@pytest.fixture
def broken():
    raise RuntimeError("falha proposital no setup")