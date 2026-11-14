"""Controls that exercise the runtime K-identical-failure circuit breaker."""

import pytest


@pytest.fixture
def repeatable_failure():
    raise RuntimeError("phase1 repeatable fixture failure")


def test_k_dependent_1(repeatable_failure):
    raise AssertionError("fixture unexpectedly succeeded")


def test_k_dependent_2(repeatable_failure):
    raise AssertionError("fixture unexpectedly succeeded")


def test_k_dependent_3(repeatable_failure):
    raise AssertionError("fixture unexpectedly succeeded")


def test_k_dependent_4(repeatable_failure):
    raise AssertionError("breaker should skip this dependent item")


def test_k_unrelated():
    assert True
