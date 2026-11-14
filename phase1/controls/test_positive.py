"""Positive-only control that can run without creating network sockets."""


def test_positive_fixture_alpha(parametrized_resource):
    assert parametrized_resource == "alpha"


def test_positive_fixture_beta(parametrized_resource):
    assert parametrized_resource == "beta"
