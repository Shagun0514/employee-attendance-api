"""Offline unit tests for the standalone benchmark's URI safety gate."""

import pytest

from scripts import benchmark_100k


def _set_opt_in(monkeypatch, uri):
    monkeypatch.setenv("RUN_100K_BENCHMARK", "1")
    if uri is None:
        monkeypatch.delenv("MONGO_BENCHMARK_URI", raising=False)
    else:
        monkeypatch.setenv("MONGO_BENCHMARK_URI", uri)
    # These values must never be used as fallbacks by the safety gate.
    monkeypatch.setenv("MONGO_URI", "mongodb://normal-db.example/attendance_db")
    monkeypatch.setenv("MONGO_DB", "attendance_db")
    monkeypatch.setenv("MONGO_TEST_URI", "mongodb://test-db.example/test")


def test_benchmark_uri_accepts_local_mongodb_uri_without_connecting(monkeypatch):
    uri = "mongodb://localhost:27017"
    _set_opt_in(monkeypatch, uri)

    assert benchmark_100k._check_required_environment() == uri


def test_benchmark_uri_accepts_srv_scheme_after_uri_validation(monkeypatch):
    uri = "mongodb+srv://cluster.example"
    _set_opt_in(monkeypatch, uri)
    # PyMongo resolves SRV records while parsing. Stub only that parser here so
    # this scheme-validation test remains offline and never contacts MongoDB/DNS.
    monkeypatch.setattr(benchmark_100k, "parse_uri", lambda value: {"database": None})

    assert benchmark_100k._check_required_environment() == uri


@pytest.mark.parametrize(
    "uri",
    ["https://localhost:27017", "not-a-mongodb-uri", "mongodb://"],
)
def test_benchmark_uri_rejects_unsupported_or_malformed_values(monkeypatch, uri):
    _set_opt_in(monkeypatch, uri)

    with pytest.raises(benchmark_100k.BenchmarkFailure):
        benchmark_100k._check_required_environment()


def test_benchmark_uri_requires_explicit_benchmark_uri(monkeypatch):
    _set_opt_in(monkeypatch, None)

    with pytest.raises(benchmark_100k.BenchmarkFailure, match="MONGO_BENCHMARK_URI"):
        benchmark_100k._check_required_environment()


def test_benchmark_uri_requires_exact_opt_in(monkeypatch):
    _set_opt_in(monkeypatch, "mongodb://localhost:27017")
    monkeypatch.setenv("RUN_100K_BENCHMARK", "true")

    with pytest.raises(benchmark_100k.BenchmarkFailure, match="RUN_100K_BENCHMARK"):
        benchmark_100k._check_required_environment()


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://localhost:27017/attendance_db",
        "mongodb://localhost:27017/employee_attendance_test_runner",
    ],
)
def test_benchmark_uri_rejects_reserved_databases(monkeypatch, uri):
    _set_opt_in(monkeypatch, uri)

    with pytest.raises(benchmark_100k.BenchmarkFailure, match="reserved"):
        benchmark_100k._check_required_environment()
