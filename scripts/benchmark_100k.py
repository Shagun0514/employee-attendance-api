r"""Explicitly opt-in, isolated benchmark for exactly 100,000 attendance rows.

Windows CMD usage (run from the project root):

    set MONGO_BENCHMARK_URI=mongodb://localhost:27017
    set RUN_100K_BENCHMARK=1
    .venv\Scripts\python.exe scripts\benchmark_100k.py

The script refuses to run without both settings. It creates a fresh
employee_attendance_benchmark_<uuid> database, verifies an ownership marker
before dropping only that database, and never uses MONGO_URI or MONGO_DB to
select a database. Do not run this against a server where you lack permission
to create and drop this uniquely named temporary database.

This is a standalone command, not a test or application startup hook. It is
intentionally not executed when imported.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time as time_module
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from pymongo.uri_parser import parse_uri


ATTENDANCE_TARGET = 100_000
EMPLOYEE_COUNT = 1_000
ROWS_PER_EMPLOYEE = ATTENDANCE_TARGET // EMPLOYEE_COUNT
INSERT_BATCH_SIZE = 5_000
REQUEST_REPEATS = 5
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
UTC = timezone.utc
APPLICATION_IMPORT_GUARD_URI = "mongodb://127.0.0.1:1/"
APPLICATION_IMPORT_GUARD_DB = "_benchmark_import_guard_unused"


class BenchmarkFailure(RuntimeError):
    """A required data, API, or correctness assertion failed."""


@dataclass(frozen=True)
class SourceRow:
    emp_code: str
    employee_number: int
    day: date
    status: str
    half_day: bool
    late_minutes: int
    overtime_minutes: int
    work_hours: float | None


def _half_up(value: float, places: int) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _assert_equal(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise BenchmarkFailure(
            f"{label} mismatch. Expected {expected!r}; received {actual!r}"
        )


def _make_source() -> tuple[list[dict], list[SourceRow], dict[str, dict]]:
    departments = [
        "Engineering", "Finance", "HR", "Operations", "Sales",
        "Support", "Product", "Legal", "Marketing", "IT",
    ]
    employees: list[dict] = []
    employees_by_code: dict[str, dict] = {}
    rows: list[SourceRow] = []
    base_day = date(2026, 1, 1)
    joined_base = date(2024, 1, 1)

    for number in range(EMPLOYEE_COUNT):
        emp_code = f"EMP{number:06d}"
        department = departments[number % len(departments)]
        joined_on = joined_base + timedelta(days=(number * 37) % 365)
        employee = {
            "emp_code": emp_code,
            "name": f"Synthetic Employee {number:04d}",
            "email": f"employee{number:04d}@benchmark.invalid",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_on.isoformat(),
            "created_at": datetime(2025, 1, 1, tzinfo=UTC)
            + timedelta(seconds=number),
        }
        employees.append(employee)
        employees_by_code[emp_code] = employee

        for offset in range(ROWS_PER_EMPLOYEE):
            day = base_day + timedelta(days=offset)
            bucket = (number * 17 + offset * 31) % 100
            if bucket < 7:
                status = "LEAVE"
            elif bucket < 14:
                status = "ABSENT"
            elif bucket < 20:
                status = "ON_DUTY"
            elif bucket < 32:
                status = "WFH"
            else:
                status = "PRESENT"

            if status in PRESENCE_STATUSES:
                lateness_candidate = (number * 3 + offset * 11) % 71
                late_minutes = lateness_candidate if lateness_candidate > 10 else 0
                duration_code = (number * 3 + offset * 5) % 20
                work_hours = 4.25 if duration_code == 0 else 9.0 if duration_code < 3 else 8.0
                half_day = work_hours < 4.5
                punch_in = datetime.combine(day, time(4, 0), tzinfo=UTC)
                if late_minutes:
                    punch_in += timedelta(minutes=late_minutes)
                punch_out = punch_in + timedelta(hours=work_hours)
                shift_end_utc = datetime.combine(day, time(13, 0), tzinfo=UTC)
                overtime_seconds = int((punch_out - shift_end_utc).total_seconds())
                overtime_minutes = (
                    overtime_seconds // 60 if overtime_seconds >= 30 * 60 else 0
                )
            else:
                late_minutes = 0
                overtime_minutes = 0
                work_hours = None
                half_day = False

            rows.append(SourceRow(
                emp_code=emp_code,
                employee_number=number,
                day=day,
                status=status,
                half_day=half_day,
                late_minutes=late_minutes,
                overtime_minutes=overtime_minutes,
                work_hours=work_hours,
            ))

    if len(employees) != EMPLOYEE_COUNT:
        raise BenchmarkFailure(f"Generated {len(employees)} employees, expected {EMPLOYEE_COUNT}")
    if len(rows) != ATTENDANCE_TARGET:
        raise BenchmarkFailure(f"Generated {len(rows)} attendance rows, expected {ATTENDANCE_TARGET}")
    if len({(row.emp_code, row.day) for row in rows}) != ATTENDANCE_TARGET:
        raise BenchmarkFailure("Synthetic source contains duplicate (emp_code, date) pairs")
    return employees, rows, employees_by_code


def _attendance_document(row: SourceRow) -> dict:
    if row.status in PRESENCE_STATUSES:
        punch_in = datetime.combine(row.day, time(4, 0), tzinfo=UTC)
        if row.late_minutes:
            punch_in += timedelta(minutes=row.late_minutes)
        punch_out = punch_in + timedelta(hours=row.work_hours or 0)
    else:
        punch_in = None
        punch_out = None

    return {
        "emp_code": row.emp_code,
        "date": row.day.isoformat(),
        "status": row.status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": row.work_hours,
        "late_minutes": row.late_minutes,
        "overtime_minutes": row.overtime_minutes,
        "half_day": row.half_day,
        "history": [],
    }


def _monthly_expected(emp_code: str, month: str, rows: list[SourceRow], employee: dict) -> dict:
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    end = next_month - timedelta(days=1)
    joined_on = date.fromisoformat(employee["joined_on"])
    workday_start = max(start, joined_on)
    working_days = sum(
        1 for offset in range((end - workday_start).days + 1)
        if (workday_start + timedelta(days=offset)).weekday() < 5
    ) if workday_start <= end else 0
    month_rows = [row for row in rows if row.emp_code == emp_code and start <= row.day <= end]
    present_days = sum(
        (0.5 if row.half_day else 1.0)
        for row in month_rows
        if row.day.weekday() < 5 and row.status in PRESENCE_STATUSES
    )
    attendance_pct = (
        _half_up(_half_up(present_days, 2) / working_days * 100, 4)
        if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _half_up(present_days, 2),
        "leave_days": sum(row.status == "LEAVE" for row in month_rows),
        "late_count": sum(row.late_minutes > 0 for row in month_rows),
        "total_late_minutes": sum(row.late_minutes for row in month_rows),
        "total_overtime_minutes": sum(row.overtime_minutes for row in month_rows),
        "attendance_pct": attendance_pct,
    }


def _department_expected(month: str, department: str, rows: list[SourceRow], employees: dict[str, dict]) -> dict:
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    codes = {
        code for code, employee in employees.items()
        if employee["department"] == department
        and date.fromisoformat(employee["joined_on"]) <= end
    }
    selected = [row for row in rows if row.emp_code in codes and start <= row.day <= end]
    present_days = sum(
        (0.5 if row.half_day else 1.0)
        for row in selected
        if row.day.weekday() < 5 and row.status in PRESENCE_STATUSES
    )
    hour_values = [
        row.work_hours for row in selected
        if row.status in PRESENCE_STATUSES and row.work_hours is not None
    ]
    return {
        "department": department,
        "headcount": len(codes),
        "present_days": _half_up(present_days, 2),
        "avg_work_hours": _half_up(sum(hour_values) / len(hour_values), 2) if hour_values else None,
        "late_count": sum(row.late_minutes > 0 for row in selected),
        "total_late_minutes": sum(row.late_minutes for row in selected),
        "leave_count": sum(row.status == "LEAVE" for row in selected),
        "on_duty_count": sum(row.status == "ON_DUTY" for row in selected),
    }


def _leaderboard_expected(month: str, department: str, limit: int, rows: list[SourceRow], employees: dict[str, dict]) -> dict:
    start = date.fromisoformat(f"{month}-01")
    end = (date(start.year + 1, 1, 1) - timedelta(days=1)) if start.month == 12 else (date(start.year, start.month + 1, 1) - timedelta(days=1))
    totals: dict[str, list[int]] = {}
    for row in rows:
        if start <= row.day <= end and row.emp_code in employees:
            if employees[row.emp_code]["department"] != department:
                continue
            values = totals.setdefault(row.emp_code, [0, 0])
            values[0] += row.late_minutes
            values[1] += row.late_minutes > 0
    ranked = sorted(
        ((code, values[0], values[1]) for code, values in totals.items() if values[0] > 0),
        key=lambda item: (-item[1], item[0]),
    )
    result = []
    prior_total = None
    rank = 0
    for position, (code, total, late_count) in enumerate(ranked, start=1):
        if total != prior_total:
            rank = position
            prior_total = total
        if rank <= limit:
            employee = employees[code]
            result.append({
                "rank": rank,
                "emp_code": code,
                "name": employee["name"],
                "department": employee["department"],
                "total_late_minutes": total,
                "late_count": late_count,
            })
    return {"month": month, "items": result}


def _trend_expected(department: str, start: date, end: date, rows: list[SourceRow], employees: dict[str, dict]) -> dict:
    department_codes = {
        code for code, employee in employees.items()
        if employee["department"] == department
    }
    raw_rates: list[float | None] = []
    items = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_rows = [row for row in rows if row.emp_code in department_codes and row.day == day]
        headcount = sum(
            date.fromisoformat(employees[code]["joined_on"]) <= day
            for code in department_codes
        )
        present_count = sum(
            (0.5 if row.half_day else 1.0)
            for row in day_rows if row.status in PRESENCE_STATUSES
        )
        working = day.weekday() < 5
        rate = present_count / headcount if working and headcount else None
        raw_rates.append(rate)
        moving_values = [value for value in raw_rates[-7:] if value is not None]
        moving_average = sum(moving_values) / len(moving_values) if moving_values else None
        items.append({
            "date": day.isoformat(),
            "is_working_day": working,
            "headcount": headcount,
            "present_count": _half_up(present_count, 2),
            "late_count": sum(row.late_minutes > 0 for row in day_rows),
            "attendance_rate": _half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": _half_up(moving_average, 4) if moving_average is not None else None,
        })
    return {"department": department, "items": items}


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _plan_summary(explain: dict) -> dict:
    index_names: set[str] = set()
    stage_names: set[str] = set()
    metrics = []
    for node in _walk(explain):
        stage = node.get("stage")
        if isinstance(stage, str):
            stage_names.add(stage)
        for key in ("indexName", "indexesUsed"):
            value = node.get(key)
            if isinstance(value, str):
                index_names.add(value)
            elif isinstance(value, list):
                index_names.update(item for item in value if isinstance(item, str))
        if "executionStats" in node and isinstance(node["executionStats"], dict):
            stats = node["executionStats"]
            metrics.append({
                key: stats[key] for key in (
                    "executionTimeMillis", "nReturned", "totalKeysExamined",
                    "totalDocsExamined",
                ) if key in stats
            })
        elif "nReturned" in node and "executionTimeMillisEstimate" in node:
            metrics.append({
                "nReturned": node.get("nReturned"),
                "executionTimeMillisEstimate": node.get("executionTimeMillisEstimate"),
            })
    return {"indexes": sorted(index_names), "stages": sorted(stage_names), "metrics": metrics}


async def _request(client: httpx.AsyncClient, path: str) -> tuple[dict, float]:
    started = time_module.perf_counter()
    response = await client.get(path)
    elapsed = time_module.perf_counter() - started
    if response.status_code >= 400:
        raise BenchmarkFailure(
            f"GET {path} returned HTTP {response.status_code}: {response.text[:1000]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise BenchmarkFailure(f"GET {path} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise BenchmarkFailure(f"GET {path} returned a non-object JSON body")
    return body, elapsed


async def _run_routes(app, cases: list[tuple[str, str, Any]]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    # HTTPX's ASGITransport does not drive application lifespan/startup. The
    # app globals are verified and its index routine is called explicitly
    # against the generated database before this function is reached.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://benchmark.local", timeout=120.0
    ) as api:
        for name, path, validator in cases:
            # Warm the application/connection once, then time five checked calls.
            warm_body, _ = await _request(api, path)
            validator(warm_body)
            durations = []
            for _ in range(REQUEST_REPEATS):
                body, elapsed = await _request(api, path)
                validator(body)
                durations.append(elapsed)
            results[name] = {
                "median_seconds": statistics.median(durations),
                "minimum_seconds": min(durations),
                "maximum_seconds": max(durations),
                "repeats": len(durations),
            }
    return results


def _check_plan(app, path: str, endpoint: str, collection: str) -> dict:
    async def execute():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://benchmark.local", timeout=120.0
        ) as api:
            response = await api.get(path)
            if response.status_code != 200:
                raise BenchmarkFailure(
                    f"GET {path} returned HTTP {response.status_code}: {response.text[:1000]}"
                )
            body = response.json()
            if set(body) != {"endpoint", "collection", "explain"}:
                raise BenchmarkFailure(f"Unexpected explain response fields for {endpoint}")
            _assert_equal(f"{endpoint} explain endpoint", body["endpoint"], endpoint)
            _assert_equal(f"{endpoint} explain collection", body["collection"], collection)
            if not isinstance(body["explain"], dict):
                raise BenchmarkFailure(f"{endpoint} explain payload is not an object")
            return body["explain"]

    return asyncio.run(execute())


def _check_required_environment() -> str:
    uri = os.environ.get("MONGO_BENCHMARK_URI")
    if not uri:
        raise BenchmarkFailure("MONGO_BENCHMARK_URI must be explicitly set")
    if os.environ.get("RUN_100K_BENCHMARK") != "1":
        raise BenchmarkFailure("RUN_100K_BENCHMARK must be exactly 1")
    try:
        # PyMongo's parse_uri validates the URI but its result does not include
        # the scheme. Read that separately instead of treating a missing
        # result field as an unsupported scheme.
        scheme = urlsplit(uri).scheme.lower()
        if scheme not in {"mongodb", "mongodb+srv"}:
            raise BenchmarkFailure(
                "MONGO_BENCHMARK_URI must use mongodb or mongodb+srv"
            )
        uri_details = parse_uri(uri)
    except (TypeError, ValueError, PyMongoError):
        raise BenchmarkFailure("MONGO_BENCHMARK_URI is not a valid MongoDB URI") from None
    uri_database = uri_details.get("database")
    if uri_database == "attendance_db" or (
        isinstance(uri_database, str)
        and uri_database.startswith("employee_attendance_test_")
    ):
        raise BenchmarkFailure(
            "MONGO_BENCHMARK_URI names a reserved application or integration-test database"
        )
    return uri


def run_benchmark() -> int:
    if __name__ != "__main__":
        print("BENCHMARK REFUSED: invoke scripts/benchmark_100k.py as a standalone command", file=sys.stderr)
        return 2
    try:
        benchmark_uri = _check_required_environment()
    except BenchmarkFailure as exc:
        print(f"BENCHMARK REFUSED: {exc}", file=sys.stderr)
        return 2

    try:
        benchmark_client = MongoClient(
            benchmark_uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
        )
    except Exception:
        print("BENCHMARK REFUSED: could not configure the benchmark MongoDB client", file=sys.stderr)
        return 2

    database_name: str | None = None
    owner_token: str | None = None
    owns_database = False
    failed = False
    cleanup_failed = False
    report: dict[str, Any] = {}

    try:
        try:
            benchmark_client.admin.command("ping")
            hello = benchmark_client.admin.command("hello")
            server_info = benchmark_client.server_info()
        except PyMongoError as exc:
            raise BenchmarkFailure(f"MongoDB ping/server inspection failed: {exc}") from exc

        print("MongoDB server inspection:")
        print({
            "address": benchmark_client.address,
            "version": server_info.get("version"),
            "replica_set": hello.get("setName"),
            "reported_host": hello.get("me"),
            "writable_primary": hello.get("isWritablePrimary"),
        })

        database_name = f"employee_attendance_benchmark_{uuid.uuid4().hex}"
        if not database_name.startswith("employee_attendance_benchmark_"):
            raise BenchmarkFailure("Generated database name failed its prefix check")
        if database_name in benchmark_client.list_database_names():
            raise BenchmarkFailure(
                f"Generated database already exists; refusing reuse: {database_name}"
            )
        owner_token = uuid.uuid4().hex
        benchmark_db = benchmark_client[database_name]
        try:
            benchmark_db["_benchmark_owner"].insert_one({
                "_id": owner_token,
                "database_name": database_name,
                "purpose": "temporary 100k attendance benchmark",
            })
            owns_database = True
        except PyMongoError as exc:
            raise BenchmarkFailure(
                f"Could not establish database ownership marker for {database_name}: {exc}"
            ) from exc

        print(f"Temporary benchmark database: {database_name}")

        # app.main creates a lazy global MongoClient during import. Give that
        # unused client a deliberately unreachable, process-local URI, then
        # replace both globals with the verified benchmark client/database.
        os.environ["MONGO_URI"] = APPLICATION_IMPORT_GUARD_URI
        os.environ["MONGO_DB"] = APPLICATION_IMPORT_GUARD_DB
        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from app import main as application

        guard_client = application.client
        if (
            application.MONGO_URI != APPLICATION_IMPORT_GUARD_URI
            or application.MONGO_DB != APPLICATION_IMPORT_GUARD_DB
            or application.db.client is not guard_client
            or application.db.name != APPLICATION_IMPORT_GUARD_DB
        ):
            raise BenchmarkFailure("Application import guard did not isolate its initial Mongo globals")
        application.client = benchmark_client
        application.db = benchmark_db
        guard_client.close()

        def verify_application_database() -> None:
            if application.client is not benchmark_client:
                raise BenchmarkFailure("Application client no longer points at benchmark MongoDB")
            if application.db.client is not benchmark_client or application.db.name != database_name:
                raise BenchmarkFailure("Application database no longer points at the generated benchmark DB")

        verify_application_database()

        generation_started = time_module.perf_counter()
        employees, source_rows, employees_by_code = _make_source()
        generation_seconds = time_module.perf_counter() - generation_started
        report["synthetic_generation_seconds"] = generation_seconds
        _assert_equal("synthetic attendance row count", len(source_rows), ATTENDANCE_TARGET)
        _assert_equal("unique employee/date pairs", len({(row.emp_code, row.day) for row in source_rows}), ATTENDANCE_TARGET)

        insert_started = time_module.perf_counter()
        employee_result = benchmark_db.employees.insert_many(employees, ordered=True)
        _assert_equal("inserted employee count", len(employee_result.inserted_ids), EMPLOYEE_COUNT)
        for offset in range(0, len(source_rows), INSERT_BATCH_SIZE):
            batch = [
                _attendance_document(row)
                for row in source_rows[offset:offset + INSERT_BATCH_SIZE]
            ]
            benchmark_db.attendance_logs.insert_many(batch, ordered=True)
        bulk_insert_seconds = time_module.perf_counter() - insert_started
        report["bulk_insert_seconds"] = bulk_insert_seconds
        final_count = benchmark_db.attendance_logs.count_documents({})
        _assert_equal("persisted attendance record count", final_count, ATTENDANCE_TARGET)

        verify_application_database()
        index_started = time_module.perf_counter()
        application.create_indexes()
        index_creation_seconds = time_module.perf_counter() - index_started
        report["index_creation_seconds"] = index_creation_seconds
        verify_application_database()

        expected_employee_indexes = {"uq_employees_emp_code", "ix_employees_department_joined"}
        expected_attendance_indexes = {
            "uq_attendance_emp_date", "ix_attendance_date_emp",
            "ix_attendance_status_date_emp",
        }
        actual_employee_indexes = set(benchmark_db.employees.index_information())
        actual_attendance_indexes = set(benchmark_db.attendance_logs.index_information())
        if not expected_employee_indexes <= actual_employee_indexes:
            raise BenchmarkFailure(f"Missing employee indexes: {expected_employee_indexes - actual_employee_indexes}")
        if not expected_attendance_indexes <= actual_attendance_indexes:
            raise BenchmarkFailure(f"Missing attendance indexes: {expected_attendance_indexes - actual_attendance_indexes}")

        selected_emp = "EMP000000"
        selected_department = "Engineering"
        selected_month = "2026-02"
        month_start = date(2026, 2, 1)
        month_end = date(2026, 2, 28)
        trend_start = date(2026, 2, 2)
        trend_end = date(2026, 2, 8)
        source_employee = employees_by_code[selected_emp]

        expected_list_rows = sorted(
            (
                row for row in source_rows
                if row.status == "LEAVE"
                and date(2026, 1, 1) <= row.day <= date(2026, 4, 10)
            ),
            key=lambda row: (row.day, row.emp_code),
        )
        expected_list_rows.sort(key=lambda row: row.day, reverse=True)
        expected_monthly = _monthly_expected(selected_emp, selected_month, source_rows, source_employee)
        expected_department = _department_expected(selected_month, selected_department, source_rows, employees_by_code)
        expected_leaderboard = _leaderboard_expected(selected_month, selected_department, 10, source_rows, employees_by_code)
        expected_trend = _trend_expected(selected_department, trend_start, trend_end, source_rows, employees_by_code)

        def validate_list(body: dict) -> None:
            _assert_equal("attendance-list total", body.get("total"), len(expected_list_rows))
            _assert_equal("attendance-list page", body.get("page"), 1)
            _assert_equal("attendance-list page_size", body.get("page_size"), 25)
            _assert_equal("attendance-list date/code page", [
                (item["date"], item["emp_code"]) for item in body.get("items", [])
            ], [(row.day.isoformat(), row.emp_code) for row in expected_list_rows[:25]])

        def validate_monthly(body: dict) -> None:
            _assert_equal("employee monthly analytics", body, expected_monthly)

        def validate_summary(body: dict) -> None:
            if body.get("month") != selected_month:
                raise BenchmarkFailure("Department summary returned the wrong month")
            items = body.get("items", [])
            matching = [item for item in items if item.get("department") == selected_department]
            _assert_equal("Engineering department summary", matching, [expected_department])

        def validate_leaderboard(body: dict) -> None:
            _assert_equal("late leaderboard", body, expected_leaderboard)

        def validate_trend(body: dict) -> None:
            _assert_equal("department trend", body, expected_trend)

        def attendance_path(base: str) -> str:
            return base + "?status=LEAVE&date_from=2026-01-01&date_to=2026-04-10&page=1&page_size=25"

        cases = [
            ("attendance_list", attendance_path("/attendance"), validate_list),
            ("employee_monthly", f"/analytics/employees/{selected_emp}/monthly?month={selected_month}", validate_monthly),
            ("department_summary", f"/analytics/departments/summary?month={selected_month}&department={selected_department}", validate_summary),
            ("late_leaderboard", f"/analytics/leaderboard/late?month={selected_month}&limit=10&department={selected_department}", validate_leaderboard),
            ("department_trend", f"/analytics/departments/{selected_department}/trend?from={trend_start.isoformat()}&to={trend_end.isoformat()}", validate_trend),
        ]

        verify_application_database()
        route_timings = asyncio.run(_run_routes(application.app, cases))
        report["endpoint_timings"] = route_timings

        plan_cases = [
            (
                "attendance_list",
                attendance_path("/admin/explain/attendance_list"),
                "attendance_logs",
                {"ix_attendance_status_date_emp"},
                False,
            ),
            (
                "employee_monthly",
                f"/admin/explain/employee_monthly?emp_code={selected_emp}&month={selected_month}",
                "attendance_logs",
                {"uq_attendance_emp_date"},
                False,
            ),
            (
                "department_summary_filtered",
                f"/admin/explain/department_summary?month={selected_month}&department={selected_department}",
                "employees",
                {"ix_employees_department_joined", "uq_attendance_emp_date"},
                False,
            ),
            (
                "department_summary_unfiltered",
                f"/admin/explain/department_summary?month={selected_month}",
                "employees",
                {"uq_attendance_emp_date"},
                True,
            ),
            (
                "late_leaderboard",
                f"/admin/explain/late_leaderboard?month={selected_month}&limit=10&department={selected_department}",
                "attendance_logs",
                {"ix_attendance_date_emp", "uq_employees_emp_code"},
                False,
            ),
            (
                "department_trend",
                f"/admin/explain/department_trend?department={selected_department}&from={trend_start.isoformat()}&to={trend_end.isoformat()}",
                "database",
                {"ix_employees_department_joined", "ix_attendance_date_emp", "uq_employees_emp_code"},
                False,
            ),
        ]
        plans = {}
        for name, path, collection, expected_indexes, all_employee_scan_expected in plan_cases:
            verify_application_database()
            endpoint = name.removesuffix("_filtered").removesuffix("_unfiltered")
            explain = _check_plan(application.app, path, endpoint, collection)
            summary = _plan_summary(explain)
            found_indexes = set(summary["indexes"])
            missing_indexes = sorted(expected_indexes - found_indexes)
            scans = [stage for stage in summary["stages"] if stage == "COLLSCAN"]
            if missing_indexes:
                assessment = f"missing expected index evidence: {missing_indexes}"
            elif scans and all_employee_scan_expected:
                assessment = "COLLSCAN may be the expected employees source scan for an unfiltered summary; lookup index evidence checked"
            elif scans:
                assessment = "unexpected COLLSCAN on a selective benchmark query"
            else:
                assessment = "expected index evidence present; no COLLSCAN stage reported"
            plans[name] = {
                **summary,
                "expected_indexes": sorted(expected_indexes),
                "missing_expected_indexes": missing_indexes,
                "scan_assessment": assessment,
            }
        report["explain_plans"] = plans
        report["record_count"] = final_count
        report["employee_count"] = len(employees)
        report["benchmark_database"] = database_name
        print("Benchmark results (timings are in-process ASGI request latency):")
        print(report)

    except Exception as exc:
        failed = True
        print(f"BENCHMARK FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
    finally:
        if owns_database and database_name and owner_token:
            try:
                if not database_name.startswith("employee_attendance_benchmark_"):
                    raise BenchmarkFailure("Refusing cleanup: generated database name prefix check failed")
                marker = benchmark_client[database_name]["_benchmark_owner"].find_one({
                    "_id": owner_token,
                    "database_name": database_name,
                    "purpose": "temporary 100k attendance benchmark",
                })
                if marker is None:
                    raise BenchmarkFailure("Refusing cleanup: ownership marker could not be verified")
                benchmark_client.drop_database(database_name)
                print(f"Cleaned up owned benchmark database: {database_name}")
            except Exception as exc:
                cleanup_failed = True
                print(
                    f"CLEANUP REFUSED/FAILED for {database_name}: {exc}. "
                    f"Inspect this generated database manually; no other database was targeted.",
                    file=sys.stderr,
                )
        elif database_name:
            print(
                f"No verified ownership marker; database was not dropped. Inspect manually: {database_name}",
                file=sys.stderr,
            )
        benchmark_client.close()

    if failed or cleanup_failed:
        print("Benchmark status: FAILED", file=sys.stderr)
        return 1
    print("Benchmark status: COMPLETED; temporary database cleanup verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_benchmark())
