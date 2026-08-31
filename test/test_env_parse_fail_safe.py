from __future__ import annotations

from swarm.config.env_parse import env_float, env_int


def test_env_int_invalid_falls_back_and_zero_is_clamped(monkeypatch):
    monkeypatch.setenv("SWARM_TEST_PARSE", "abc")
    assert env_int("SWARM_TEST_PARSE", 2, minimum=1) == 2
    monkeypatch.setenv("SWARM_TEST_PARSE", "0")
    assert env_int("SWARM_TEST_PARSE", 2, minimum=1) == 1


def test_env_float_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SWARM_TEST_PARSE", "bad")
    assert env_float("SWARM_TEST_PARSE", 3.5, minimum=1.0) == 3.5


def test_env_float_non_finite_falls_back(monkeypatch):
    for raw in ("nan", "inf", "-inf"):
        monkeypatch.setenv("SWARM_TEST_PARSE", raw)
        assert env_float("SWARM_TEST_PARSE", 3.5, minimum=1.0) == 3.5
