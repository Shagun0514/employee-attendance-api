import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from pymongo import MongoClient
from pymongo.errors import ConfigurationError, DuplicateKeyError, PyMongoError
from pymongo.uri_parser import parse_uri

from app import main


MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


class StubCollection:
    def __init__(self, *, find_one=None, insert_one=None, aggregate=None):
        self._find_one = find_one or (lambda *args, **kwargs: None)
        self._insert_one = insert_one or (lambda document: None)
        self._aggregate = aggregate or (lambda pipeline: [])

    def find_one(self, *args, **kwargs):
        return self._find_one(*args, **kwargs)

    def insert_one(self, document):
        return self._insert_one(document)

    def aggregate(self, pipeline):
        return self._aggregate(pipeline)


class StubDatabase:
    def __init__(self, employees, attendance_logs):
        self.employees = employees
        self.attendance_logs = attendance_logs


class RecordedCursor:
    def __init__(self, cursor, operation):
        self.cursor = cursor
        self.operation = operation

    def sort(self, keys, direction=None):
        self.operation["sort"] = dict(keys) if not isinstance(keys, str) else {keys: direction}
        if direction is None:
            self.cursor.sort(keys)
        else:
            self.cursor.sort(keys, direction)
        return self

    def skip(self, count):
        self.operation["skip"] = count
        self.cursor.skip(count)
        return self

    def limit(self, count):
        self.operation["limit"] = count
        self.cursor.limit(count)
        return self

    def __iter__(self):
        return iter(self.cursor)


class RecordedCollection:
    def __init__(self, collection, name, recorder):
        self.collection = collection
        self.name = name
        self.recorder = recorder

    def find(self, filter=None, projection=None, *args, **kwargs):
        operation = {
            "kind": "find", "collection": self.name,
            "filter": filter or {}, "projection": projection,
        }
        self.recorder.actual_operation = operation
        return RecordedCursor(
            self.collection.find(filter, projection, *args, **kwargs), operation
        )

    def aggregate(self, pipeline, *args, **kwargs):
        self.recorder.actual_operation = {
            "kind": "aggregate", "collection": self.name,
            "pipeline": pipeline,
        }
        return self.collection.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.collection, name)


class RecordedDatabase:
    """Delegate to the isolated Mongo DB while recording endpoint query inputs."""

    def __init__(self, database):
        self.database = database
        self.actual_operation = None
        self.explain_operation = None
        self.explain_verbosity = None

    def __getattr__(self, name):
        if name in {"employees", "attendance_logs"}:
            return RecordedCollection(getattr(self.database, name), name, self)
        return getattr(self.database, name)

    def aggregate(self, pipeline, *args, **kwargs):
        self.actual_operation = {
            "kind": "aggregate", "collection": "database",
            "pipeline": pipeline,
        }
        return self.database.aggregate(pipeline, *args, **kwargs)

    def command(self, command, *args, **kwargs):
        if isinstance(command, dict) and "explain" in command:
            self.explain_operation = command["explain"]
            self.explain_verbosity = command.get("verbosity")
        return self.database.command(command, *args, **kwargs)


def explain_index_names(value):
    names = set()

    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"indexName", "indexesUsed"}:
                    if isinstance(child, str):
                        names.add(child)
                    elif isinstance(child, list):
                        names.update(item for item in child if isinstance(item, str))
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return names


def explain_stage_names(value):
    stages = set()

    def visit(node):
        if isinstance(node, dict):
            stage = node.get("stage")
            if isinstance(stage, str):
                stages.add(stage)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return stages


def has_execution_stats(value):
    if isinstance(value, dict):
        if "executionStats" in value:
            return True
        # Database-level $documents aggregation explains on MongoDB 8 can
        # expose per-stage execution metrics directly without an
        # executionStats wrapper.
        if "nReturned" in value and any(
            key in value for key in ("executionTimeMillisEstimate", "executionTimeMillis")
        ):
            return True
        return any(has_execution_stats(child) for child in value.values())
    if isinstance(value, list):
        return any(has_execution_stats(child) for child in value)
    return False


def request(method, path, *, json=None):
    async def send():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json)

    return asyncio.run(send())


def employee_document():
    return {
        "emp_code": "EMP0001",
        "name": "Test Employee",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2020-01-01",
    }


@pytest.fixture
def employee_db(monkeypatch):
    inserted = []
    employees = StubCollection(
        find_one=lambda *args, **kwargs: employee_document(),
        insert_one=lambda document: inserted.append(document),
    )
    attendance = StubCollection()
    monkeypatch.setattr(main, "db", StubDatabase(employees, attendance))
    return inserted


@pytest.mark.parametrize("route", ["/attendance/punch-in", "/attendance/punch-out"])
@pytest.mark.parametrize("bad_timestamp", [None, "1783312500000", 1783312500000.0, True, MIN_EPOCH_MS - 1, MAX_EPOCH_MS + 1])
def test_punch_routes_reject_invalid_or_null_timestamps(employee_db, route, bad_timestamp):
    response = request("POST", route, json={"emp_code": "EMP0001", "punched_at": bad_timestamp})
    assert response.status_code == 422


@pytest.mark.parametrize("timestamp", [MIN_EPOCH_MS, MAX_EPOCH_MS])
def test_punch_in_accepts_inclusive_timestamp_bounds(employee_db, timestamp):
    response = request("POST", "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
    assert response.status_code == 201
    assert response.json()["punch_in"] == timestamp


def test_punch_in_omitted_timestamp_still_uses_current_time(employee_db):
    before = int(datetime.now(timezone.utc).timestamp()) * 1000
    response = request("POST", "/attendance/punch-in", json={"emp_code": "EMP0001"})
    after = int(datetime.now(timezone.utc).timestamp()) * 1000
    assert response.status_code == 201
    assert before <= response.json()["punch_in"] <= after


def test_punch_out_omitted_timestamp_still_updates_open_record(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    record = {
        "_id": "attendance-1",
        "emp_code": "EMP0001",
        "date": now.astimezone(main.IST).date().isoformat(),
        "status": "PRESENT",
        "punch_in": now - timedelta(hours=1),
        "punch_out": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    updates = []

    def update(query, update, **kwargs):
        updates.append(query)
        return {**record, **update["$set"]}

    attendance = StubCollection(find_one=lambda *args, **kwargs: record)
    attendance.find_one_and_update = update
    monkeypatch.setattr(main, "db", StubDatabase(
        StubCollection(find_one=lambda *args, **kwargs: employee_document()), attendance
    ))

    response = request("POST", "/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert response.status_code == 200
    assert response.json()["punch_out"] is not None
    assert updates == [{"_id": "attendance-1", "punch_out": None}]


@pytest.mark.parametrize("bad_joined_on", [1791849600, 1791849600.0, "2026-10-13T00:00:00", "2026-02-30", "2026-1-3"])
def test_employee_rejects_non_date_string_joined_on(employee_db, bad_joined_on):
    body = employee_document()
    body["joined_on"] = bad_joined_on
    response = request("POST", "/employees", json=body)
    assert response.status_code == 422
    assert employee_db == []


def test_employee_accepts_date_string_joined_on(employee_db):
    response = request("POST", "/employees", json=employee_document())
    assert response.status_code == 201
    assert response.json()["joined_on"] == "2020-01-01"


@pytest.mark.parametrize("field", ["status", "punch_in", "punch_out"])
def test_regularization_rejects_explicit_null_optional_fields(field):
    body = {"reason": "Approved correction", "regularized_by": "manager"}
    body[field] = None
    response = request("PATCH", "/attendance/EMP0001/2026-10-13", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("field", ["punch_in", "punch_out"])
def test_regularization_rejects_null_punch_fields_for_non_present_status(status, field):
    body = {"reason": "Approved correction", "regularized_by": "manager", "status": status, field: None}
    response = request("PATCH", "/attendance/EMP0001/2026-10-13", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("existing_status", "target_status"),
    [("PRESENT", "ABSENT"), ("PRESENT", "LEAVE"), ("WFH", "PRESENT")],
)
def test_regularization_omitted_punch_fields_remain_valid(
    monkeypatch, existing_status, target_status
):
    punch_in = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
    current = {
        "_id": "attendance-2", "emp_code": "EMP0001", "date": "2026-10-13",
        "status": existing_status, "punch_in": punch_in,
        "punch_out": punch_in + timedelta(hours=8) if existing_status == "PRESENT" else None,
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    saved = {}
    pushed = []
    updates = []

    def find_attendance(*args, **kwargs):
        if not saved:
            return current
        return {**current, **saved, "history": current["history"] + pushed}

    def update_one(query, update):
        updates.append((query, update))
        saved.update(update["$set"])
        pushed.append(update["$push"]["history"])
        return type("UpdateResult", (), {"modified_count": 1})()

    attendance = StubCollection(find_one=find_attendance)
    attendance.update_one = update_one
    monkeypatch.setattr(main, "db", StubDatabase(
        StubCollection(find_one=lambda *args, **kwargs: employee_document()), attendance
    ))
    response = request("PATCH", "/attendance/EMP0001/2026-10-13", json={
        "status": target_status, "reason": "Approved change", "regularized_by": "manager"
    })
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == target_status
    assert len(updates) == 1
    if target_status in ("ABSENT", "LEAVE"):
        assert result["punch_in"] is None
        assert result["punch_out"] is None
    else:
        assert result["punch_in"] is not None
        assert result["punch_out"] is None


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("field", ["punch_in", "punch_out"])
def test_regularization_rejects_non_null_punch_fields_for_non_present_status(
    monkeypatch, status, field
):
    current = {
        "_id": "attendance-business-rule", "emp_code": "EMP0001",
        "date": "2026-10-13", "status": "PRESENT",
        "punch_in": datetime(2026, 10, 13, 4, tzinfo=timezone.utc),
        "punch_out": None, "work_hours": None, "late_minutes": 0,
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    reads = []
    writes = []

    def find_attendance(query, **kwargs):
        reads.append(query)
        return current

    attendance = StubCollection(find_one=find_attendance)
    attendance.update_one = lambda *args, **kwargs: writes.append(args)
    monkeypatch.setattr(main, "db", StubDatabase(
        StubCollection(find_one=lambda *args, **kwargs: employee_document()), attendance
    ))
    body = {
        "status": status, "reason": "Approved change",
        "regularized_by": "manager", field: 1_791_849_600_000,
    }

    response = request("PATCH", "/attendance/EMP0001/2026-10-13", json=body)

    assert response.status_code == 422
    assert len(reads) == 1  # The request passed Pydantic validation and reached the handler.
    assert writes == []


def test_monthly_leave_pipeline_expression_counts_leave_status_without_weekday_filter():
    pipeline = main.build_employee_monthly_pipeline(
        "EMP0001", datetime(2026, 10, 1).date(), datetime(2026, 10, 31).date()
    )
    group = next(stage["$group"] for stage in pipeline if "$group" in stage)
    assert group["leave_days"] == {
        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
    }


@pytest.fixture
def isolated_mongodb(monkeypatch):
    test_uri = os.getenv("MONGO_TEST_URI")
    if not test_uri:
        pytest.skip("MONGO_TEST_URI is unset; isolated MongoDB integration test not configured")
    if os.getenv("RUN_MONGO_INTEGRATION") != "1":
        pytest.skip("set RUN_MONGO_INTEGRATION=1 to opt in to MongoDB integration tests")

    try:
        uri_options = parse_uri(test_uri)
    except (TypeError, ValueError):
        pytest.fail("MONGO_TEST_URI is not a valid MongoDB URI; no database was selected")
    if uri_options.get("database") in {main.MONGO_DB, "attendance_db"}:
        pytest.fail("MONGO_TEST_URI names the application's configured database; refusing to connect")

    try:
        client = MongoClient(
            test_uri, tz_aware=True, serverSelectionTimeoutMS=3000
        )
    except ConfigurationError:
        pytest.fail("MONGO_TEST_URI has invalid client options; no database was selected")

    try:
        try:
            client.admin.command("ping")
        except PyMongoError:
            pytest.skip("MONGO_TEST_URI ping failed; no test database was created")

        owns_database = False
        database_name = f"employee_attendance_test_{uuid.uuid4().hex}"
        owner_token = uuid.uuid4().hex

        if database_name == main.MONGO_DB or not database_name.startswith("employee_attendance_test_"):
            pytest.fail("generated integration database name failed its safety check")
        if database_name in client.list_database_names():
            pytest.fail("generated integration database already exists; refusing to use it")

        database = client[database_name]
        database["_integration_test_owner"].insert_one(
            {"_id": owner_token, "database_name": database_name}
        )
        owns_database = True

        # Initialize precisely the indexes used at application startup, but with
        # globals redirected to this test-owned database before calling the hook.
        monkeypatch.setattr(main, "client", client)
        monkeypatch.setattr(main, "db", database)
        main.create_indexes()

        yield database
    finally:
        try:
            if locals().get("owns_database", False):
                marker = client[database_name]["_integration_test_owner"].find_one(
                    {"_id": owner_token, "database_name": database_name}
                )
                if marker is None:
                    pytest.fail("test database ownership marker is missing; refusing to drop database")
                client.drop_database(database_name)
        finally:
            client.close()


@pytest.mark.integration
def test_monthly_leave_days_counts_weekend_leave_in_mongodb(isolated_mongodb):
    isolated_mongodb.attendance_logs.insert_many([
        {
            "emp_code": "EMPTEST0001", "date": "2026-10-10",
            "status": "LEAVE", "half_day": False,
            "late_minutes": 0, "overtime_minutes": 0,
        },  # Saturday leave
        {
            "emp_code": "EMPTEST0001", "date": "2026-10-12",
            "status": "LEAVE", "half_day": False,
            "late_minutes": 0, "overtime_minutes": 0,
        },  # Monday leave control
        {
            "emp_code": "EMPTEST0001", "date": "2026-10-13",
            "status": "PRESENT", "half_day": False,
            "late_minutes": 0, "overtime_minutes": 0,
        },  # Present control
    ])
    pipeline = main.build_employee_monthly_pipeline(
        "EMPTEST0001", date(2026, 10, 1), date(2026, 10, 31)
    )
    result = next(isolated_mongodb.attendance_logs.aggregate(pipeline), None)

    assert result is not None
    assert result["leave_days"] == 2
    assert result["present_days"] == 1


@pytest.mark.integration
def test_explain_branches_match_endpoint_queries_and_use_supporting_indexes_in_mongodb(
    isolated_mongodb, monkeypatch,
):
    # Give the planner selective predicates and enough records to make the
    # relevant existing indexes useful. This is a plan check, not a scale test.
    employee_docs = []
    attendance_docs = []
    months = [(year, month) for year in (2025, 2026) for month in range(1, 13)]
    for number in range(120):
        emp_code = f"EMP{number:04d}"
        department = "Engineering" if number % 10 == 0 else f"Department-{number % 10}"
        employee_docs.append({
            **employee_document(), "emp_code": emp_code, "name": emp_code,
            "email": f"{emp_code.lower()}@example.com", "department": department,
        })
        for year, month in months:
            day = f"{year:04d}-{month:02d}-15"
            status = "LEAVE" if month == 1 and number % 10 == 0 else "PRESENT"
            attendance_docs.append({
                "emp_code": emp_code, "date": day, "status": status,
                "half_day": False, "late_minutes": number % 41,
                "overtime_minutes": 0, "work_hours": 8.0,
            })
        for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
            attendance_docs.append({
                "emp_code": emp_code, "date": day, "status": "PRESENT",
                "half_day": False, "late_minutes": number % 41,
                "overtime_minutes": 0, "work_hours": 8.0,
            })

    isolated_mongodb.employees.insert_many(employee_docs)
    isolated_mongodb.attendance_logs.insert_many(attendance_docs)

    recorded_db = RecordedDatabase(isolated_mongodb)
    monkeypatch.setattr(main, "db", recorded_db)

    # Employee listing has no admin explain branch. Explain the exact captured
    # find/sort/page shape issued by its real route.
    response = request("GET", "/employees?page=1&page_size=20")
    assert response.status_code == 200
    employee_list_query = recorded_db.actual_operation
    assert employee_list_query["kind"] == "find"
    employee_list_plan = isolated_mongodb.command({
        "explain": {
            "find": "employees",
            "filter": employee_list_query["filter"],
            "projection": employee_list_query["projection"],
            "sort": employee_list_query["sort"],
            "skip": employee_list_query["skip"],
            "limit": employee_list_query["limit"],
        },
        "verbosity": "executionStats",
    })
    assert has_execution_stats(employee_list_plan)
    assert "uq_employees_emp_code" in explain_index_names(employee_list_plan)
    assert "COLLSCAN" not in explain_stage_names(employee_list_plan)

    def check_explain_pair(actual_path, explain_path, endpoint, collection, indexes):
        recorded_db.actual_operation = None
        recorded_db.explain_operation = None
        recorded_db.explain_verbosity = None
        actual_response = request("GET", actual_path)
        assert actual_response.status_code == 200
        actual_query = recorded_db.actual_operation
        assert actual_query is not None

        recorded_db.actual_operation = None
        recorded_db.explain_operation = None
        recorded_db.explain_verbosity = None
        explain_response = request("GET", explain_path)
        assert explain_response.status_code == 200
        body = explain_response.json()
        assert set(body) == {"endpoint", "collection", "explain"}
        assert body["endpoint"] == endpoint
        assert body["collection"] == collection
        assert isinstance(body["explain"], dict)
        assert recorded_db.explain_verbosity == "executionStats"
        assert has_execution_stats(body["explain"])

        explained_operation = recorded_db.explain_operation
        assert explained_operation is not None
        if actual_query["kind"] == "find":
            assert explained_operation["find"] == actual_query["collection"]
            for field in ("filter", "projection", "sort", "skip", "limit"):
                assert explained_operation[field] == actual_query[field]
        else:
            expected_collection = (
                1 if actual_query["collection"] == "database"
                else actual_query["collection"]
            )
            assert explained_operation["aggregate"] == expected_collection
            assert explained_operation["pipeline"] == actual_query["pipeline"]

        used_indexes = explain_index_names(body["explain"])
        assert indexes <= used_indexes, (endpoint, indexes, used_indexes)
        assert "COLLSCAN" not in explain_stage_names(body["explain"]), endpoint

    check_explain_pair(
        "/attendance?status=LEAVE&date_from=2025-01-01&date_to=2025-01-31&page_size=100",
        "/admin/explain/attendance_list?status=LEAVE&date_from=2025-01-01&date_to=2025-01-31&page_size=100",
        "attendance_list", "attendance_logs", {"ix_attendance_status_date_emp"},
    )
    check_explain_pair(
        "/analytics/employees/EMP0000/monthly?month=2026-10",
        "/admin/explain/employee_monthly?emp_code=EMP0000&month=2026-10",
        "employee_monthly", "attendance_logs", {"uq_attendance_emp_date"},
    )
    check_explain_pair(
        "/analytics/departments/summary?month=2026-10&department=Engineering",
        "/admin/explain/department_summary?month=2026-10&department=Engineering",
        "department_summary", "employees",
        {"ix_employees_department_joined", "uq_attendance_emp_date"},
    )
    check_explain_pair(
        "/analytics/leaderboard/late?month=2026-10&limit=5&department=Engineering",
        "/admin/explain/late_leaderboard?month=2026-10&limit=5&department=Engineering",
        "late_leaderboard", "attendance_logs",
        {"ix_attendance_date_emp", "uq_employees_emp_code"},
    )
    check_explain_pair(
        "/analytics/departments/Engineering/trend?from=2026-10-12&to=2026-10-14",
        "/admin/explain/department_trend?department=Engineering&from=2026-10-12&to=2026-10-14",
        "department_trend", "database",
        {"ix_employees_department_joined", "ix_attendance_date_emp",
         "uq_employees_emp_code"},
    )


@pytest.mark.integration
def test_employee_monthly_calculations_and_month_boundaries_in_mongodb(
    isolated_mongodb,
):
    isolated_mongodb.employees.insert_one({
        **employee_document(), "joined_on": "2026-10-10",
    })
    records = [
        ("2026-10-10", "LEAVE", False, 0, 0, None),
        ("2026-10-12", "PRESENT", True, 11, 0, None),
        ("2026-10-14", "LEAVE", False, 0, 0, None),
        ("2026-10-30", "ON_DUTY", False, 0, 20, None),
        ("2026-10-31", "WFH", False, 12, 30, None),
        ("2026-11-01", "PRESENT", False, 100, 100, None),
    ]
    isolated_mongodb.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001", "date": day, "status": status,
            "half_day": half_day, "late_minutes": late,
            "overtime_minutes": overtime, "work_hours": hours,
        }
        for day, status, half_day, late, overtime, hours in records
    ])

    response = request(
        "GET", "/analytics/employees/EMP0001/monthly?month=2026-10"
    )

    assert response.status_code == 200
    assert response.json() == {
        "emp_code": "EMP0001", "month": "2026-10", "working_days": 15,
        "present_days": 1.5, "leave_days": 2, "late_count": 2,
        "total_late_minutes": 23, "total_overtime_minutes": 50,
        "attendance_pct": 10.0,
    }


@pytest.mark.integration
def test_department_summary_calculations_include_zero_log_headcount_in_mongodb(
    isolated_mongodb,
):
    employees = [
        {**employee_document(), "joined_on": "2020-01-01"},
        {**employee_document(), "emp_code": "EMP0002", "name": "No logs",
         "email": "no-logs@example.com", "joined_on": "2026-10-25"},
        {**employee_document(), "emp_code": "EMP0003", "name": "Future joiner",
         "email": "future@example.com", "joined_on": "2026-11-01"},
        {**employee_document(), "emp_code": "EMP0004", "name": "No attendance department",
         "email": "hr@example.com", "department": "HR"},
    ]
    isolated_mongodb.employees.insert_many(employees)
    isolated_mongodb.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-10-12", "status": "PRESENT",
         "half_day": True, "work_hours": 4.49, "late_minutes": 10},
        {"emp_code": "EMP0001", "date": "2026-10-13", "status": "ON_DUTY",
         "half_day": False, "work_hours": 8.01, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-10-14", "status": "LEAVE",
         "half_day": False, "work_hours": None, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-10-15", "status": "ABSENT",
         "half_day": False, "work_hours": None, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-10-31", "status": "WFH",
         "half_day": False, "work_hours": 9.25, "late_minutes": 5},
        {"emp_code": "EMP0003", "date": "2026-10-20", "status": "PRESENT",
         "half_day": False, "work_hours": 8, "late_minutes": 40},
    ])

    response = request("GET", "/analytics/departments/summary?month=2026-10")

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-10",
        "items": [
            {
                "department": "Engineering", "headcount": 2,
                "present_days": 1.5, "avg_work_hours": 7.25,
                "late_count": 2, "total_late_minutes": 15,
                "leave_count": 1, "on_duty_count": 1,
            },
            {
                "department": "HR", "headcount": 1,
                "present_days": 0, "avg_work_hours": None,
                "late_count": 0, "total_late_minutes": 0,
                "leave_count": 0, "on_duty_count": 0,
            },
        ],
    }


@pytest.mark.integration
def test_late_leaderboard_ranks_ties_after_department_filter_in_mongodb(
    isolated_mongodb,
):
    employees = []
    for code, department in [
        ("EMP0001", "Engineering"), ("EMP0002", "Engineering"),
        ("EMP0003", "Engineering"), ("EMP0004", "Engineering"),
        ("EMP0005", "HR"), ("EMP0006", "Engineering"),
    ]:
        employees.append({
            **employee_document(), "emp_code": code, "name": code,
            "email": f"{code.lower()}@example.com", "department": department,
        })
    isolated_mongodb.employees.insert_many(employees)
    logs = []
    for code, day, minutes in [
        ("EMP0001", "2026-10-12", 10),
        ("EMP0001", "2026-10-13", 10),
        ("EMP0002", "2026-10-12", 15),
        ("EMP0003", "2026-10-12", 15),
        ("EMP0004", "2026-10-12", 5),
        ("EMP0005", "2026-10-12", 100),
        ("EMP0006", "2026-10-12", 0),
        ("UNKNOWN", "2026-10-12", 200),
    ]:
        logs.append({
            "emp_code": code, "date": day, "late_minutes": minutes,
        })
    logs.append({
        "emp_code": "EMP0004", "date": "2026-11-01", "late_minutes": 500,
    })
    isolated_mongodb.attendance_logs.insert_many(logs)

    response = request(
        "GET", "/analytics/leaderboard/late?month=2026-10&limit=2&department=Engineering"
    )

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-10",
        "items": [
            {"rank": 1, "emp_code": "EMP0001", "name": "EMP0001",
             "department": "Engineering", "total_late_minutes": 20, "late_count": 2},
            {"rank": 2, "emp_code": "EMP0002", "name": "EMP0002",
             "department": "Engineering", "total_late_minutes": 15, "late_count": 1},
            {"rank": 2, "emp_code": "EMP0003", "name": "EMP0003",
             "department": "Engineering", "total_late_minutes": 15, "late_count": 1},
        ],
    }


@pytest.mark.integration
def test_department_trend_values_gap_filling_and_moving_average_in_mongodb(
    isolated_mongodb,
):
    employees = [
        {**employee_document(), "joined_on": "2026-10-10"},
        {**employee_document(), "emp_code": "EMP0002", "name": "Joined Monday",
         "email": "monday@example.com", "joined_on": "2026-10-12"},
        {**employee_document(), "emp_code": "EMP0003", "name": "Future joiner",
         "email": "future@example.com", "joined_on": "2026-10-14"},
    ]
    isolated_mongodb.employees.insert_many(employees)
    isolated_mongodb.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-10-10", "status": "WFH",
         "half_day": True, "late_minutes": 5},
        {"emp_code": "EMP0001", "date": "2026-10-12", "status": "PRESENT",
         "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0002", "date": "2026-10-12", "status": "ON_DUTY",
         "half_day": True, "late_minutes": 2},
    ])

    response = request(
        "GET", "/analytics/departments/Engineering/trend?from=2026-10-09&to=2026-10-13"
    )

    assert response.status_code == 200
    assert response.json() == {
        "department": "Engineering",
        "items": [
            {"date": "2026-10-09", "is_working_day": True, "headcount": 0,
             "present_count": 0, "late_count": 0, "attendance_rate": None,
             "moving_avg_7d": None},
            {"date": "2026-10-10", "is_working_day": False, "headcount": 1,
             "present_count": 0.5, "late_count": 1, "attendance_rate": None,
             "moving_avg_7d": None},
            {"date": "2026-10-11", "is_working_day": False, "headcount": 1,
             "present_count": 0, "late_count": 0, "attendance_rate": None,
             "moving_avg_7d": None},
            {"date": "2026-10-12", "is_working_day": True, "headcount": 2,
             "present_count": 1.5, "late_count": 1, "attendance_rate": 0.75,
             "moving_avg_7d": 0.75},
            {"date": "2026-10-13", "is_working_day": True, "headcount": 2,
             "present_count": 0, "late_count": 0, "attendance_rate": 0.0,
             "moving_avg_7d": 0.375},
        ],
    }


@pytest.mark.integration
def test_punch_out_closed_newest_record_does_not_fall_back_in_mongodb(
    isolated_mongodb,
):
    isolated_mongodb.employees.insert_one({
        **employee_document(), "shift_start": "22:00", "shift_end": "06:00",
    })
    older_open = {
        "emp_code": "EMP0001", "date": "2026-10-12", "status": "PRESENT",
        # 05:00 IST on Oct 13 belongs to the previous overnight shift date.
        "punch_in": datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc),
        "punch_out": None, "work_hours": None, "late_minutes": 0,
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    newer_closed = {
        "emp_code": "EMP0001", "date": "2026-10-13", "status": "PRESENT",
        "punch_in": datetime(2026, 10, 13, 16, 30, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc),
        "work_hours": 1.0, "late_minutes": 0, "overtime_minutes": 0,
        "half_day": True, "history": [],
    }
    isolated_mongodb.attendance_logs.insert_many([older_open, newer_closed])
    before = {
        day: isolated_mongodb.attendance_logs.find_one(
            {"emp_code": "EMP0001", "date": day}
        )
        for day in ("2026-10-12", "2026-10-13")
    }
    punched_at = int(datetime(2026, 10, 13, 18, tzinfo=timezone.utc).timestamp() * 1000)
    response = request("POST", "/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": punched_at,
    })

    assert response.status_code == 409
    assert response.json()["detail"] == "Attendance record is already closed"
    after = {
        day: isolated_mongodb.attendance_logs.find_one(
            {"emp_code": "EMP0001", "date": day}
        )
        for day in ("2026-10-12", "2026-10-13")
    }
    assert after == before
    assert after["2026-10-12"]["punch_out"] is None
    assert after["2026-10-13"]["punch_out"] == newer_closed["punch_out"]
    assert isolated_mongodb.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 2


@pytest.mark.integration
def test_unique_attendance_index_and_punch_in_duplicate_response_in_mongodb(
    isolated_mongodb,
):
    isolated_mongodb.employees.insert_one(employee_document())
    existing = {
        "emp_code": "EMP0001", "date": "2026-10-14", "status": "PRESENT",
        "punch_in": datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc),
        "punch_out": None, "work_hours": None, "late_minutes": 0,
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    isolated_mongodb.attendance_logs.insert_one(existing)

    duplicate_document = {key: value for key, value in existing.items() if key != "_id"}
    duplicate_document["punch_in"] = datetime(2026, 10, 14, 5, tzinfo=timezone.utc)
    with pytest.raises(DuplicateKeyError) as duplicate:
        isolated_mongodb.attendance_logs.insert_one(duplicate_document)
    assert duplicate.value.details["keyPattern"] == {"emp_code": 1, "date": 1}

    punched_at = int(datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc).timestamp() * 1000)
    response = request("POST", "/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": punched_at,
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "Attendance already exists for this employee and date"
    assert isolated_mongodb.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1
    assert isolated_mongodb.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-14"}
    )["punch_in"] == existing["punch_in"]


@pytest.mark.integration
def test_regularization_history_persists_once_and_invalid_request_does_not_append(
    isolated_mongodb,
):
    isolated_mongodb.employees.insert_one(employee_document())
    original = {
        "emp_code": "EMP0001", "date": "2026-10-13", "status": "PRESENT",
        "punch_in": datetime(2026, 10, 13, 4, 45, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
        "work_hours": 7.25, "late_minutes": 45, "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    isolated_mongodb.attendance_logs.insert_one(original)
    corrected_punch_in = int(
        datetime(2026, 10, 13, 4, 5, tzinfo=timezone.utc).timestamp() * 1000
    )

    response = request(
        "PATCH", "/attendance/EMP0001/2026-10-13", json={
            "punch_in": corrected_punch_in,
            "reason": "Correct punch time",
            "regularized_by": "supervisor",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == corrected_punch_in
    assert body["punch_out"] == int(original["punch_out"].timestamp() * 1000)
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 7.92
    persisted = isolated_mongodb.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert len(persisted["history"]) == 1
    history = persisted["history"][0]
    assert set(history) == {"at", "by", "reason", "changes"}
    assert isinstance(history["at"], datetime)
    assert history["by"] == "supervisor"
    assert history["reason"] == "Correct punch time"
    assert set(history["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert history["changes"]["punch_in"] == {
        "from": int(original["punch_in"].timestamp() * 1000),
        "to": corrected_punch_in,
    }
    assert history["changes"]["late_minutes"] == {"from": 45, "to": 0}
    assert history["changes"]["work_hours"] == {"from": 7.25, "to": 7.92}

    invalid = request(
        "PATCH", "/attendance/EMP0001/2026-10-13", json={
            "status": "LEAVE",
            "punch_out": 1_791_849_600_000,
            "reason": "Invalid leave punch",
            "regularized_by": "supervisor",
        },
    )
    assert invalid.status_code == 422
    unchanged = isolated_mongodb.attendance_logs.find_one(
        {"emp_code": "EMP0001", "date": "2026-10-13"}
    )
    assert unchanged == persisted


def test_punch_out_selects_latest_even_if_closed_and_does_not_fallback(monkeypatch):
    closed_latest = {
        "_id": "newest", "punch_in": datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 10, 13, 18, tzinfo=timezone.utc),
    }
    queries = []
    updates = []

    def find_one(query, **kwargs):
        queries.append((query, kwargs))
        return closed_latest

    attendance = StubCollection(find_one=find_one)
    attendance.find_one_and_update = lambda *args, **kwargs: updates.append(args)
    monkeypatch.setattr(main, "db", StubDatabase(StubCollection(), attendance))
    timestamp = int(datetime(2026, 10, 13, 20, tzinfo=timezone.utc).timestamp() * 1000)
    response = request("POST", "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp})

    assert response.status_code == 409
    assert queries[0][0] == {"emp_code": "EMP0001", "punch_in": {"$lte": datetime.fromtimestamp(timestamp / 1000, timezone.utc)}}
    assert queries[0][1]["sort"] == [("punch_in", main.DESCENDING)]
    assert updates == []


def test_punch_out_conditional_update_guard_returns_conflict(monkeypatch):
    punch_in = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
    record = {
        "_id": "open", "date": "2026-10-13", "punch_in": punch_in,
        "punch_out": None,
    }
    seen = []
    attendance = StubCollection(find_one=lambda *args, **kwargs: record)
    attendance.find_one_and_update = lambda query, update, **kwargs: (seen.append(query), None)[1]
    monkeypatch.setattr(main, "db", StubDatabase(
        StubCollection(find_one=lambda *args, **kwargs: employee_document()), attendance
    ))
    timestamp = int(datetime(2026, 10, 13, 18, tzinfo=timezone.utc).timestamp() * 1000)
    response = request("POST", "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp})
    assert response.status_code == 409
    assert seen == [{"_id": "open", "punch_out": None}]
