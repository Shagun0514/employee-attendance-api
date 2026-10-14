# Employee Attendance & Analytics API

A FastAPI and MongoDB implementation of the Employee Attendance & Analytics API contract in the supplied `openapi.yaml`.
The application is in `app/main.py`; the original specification is the source of truth for request and response behavior.

## Requirements

- Windows with Python 3.12
- MongoDB reachable from the development machine
- Dependencies in `requirements.txt` and `requirements-dev.txt`

## Setup in Windows CMD

From the project root:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Configure MongoDB for the application. Either set variables in the current CMD window:

```bat
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
```

Or put them in a local `.env` file in the project root (the file is ignored by Git):

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

`MONGO_URI` is the MongoDB connection URI; `MONGO_DB` selects the application database and defaults to `attendance_db`.
Keep real credentials local and do not commit `.env`.

## Run the API

With MongoDB available and the environment configured:

```bat
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The application creates its indexes idempotently on startup. The readiness endpoint is `GET http://127.0.0.1:8000/health`.
Interactive Swagger UI is at `http://127.0.0.1:8000/docs`; the generated OpenAPI document is at
`http://127.0.0.1:8000/openapi.json`.

## API operations

All errors use FastAPI's `{"detail": ...}` response shape. Request validation errors return 422.

| Method and path | Behavior |
|---|---|
| `GET /health` | MongoDB readiness check; returns 200 when MongoDB responds and 503 otherwise. |
| `POST /employees` | Creates an employee; `emp_code`, name, email, department, and `joined_on` are required. Shift times default to 09:30 and 18:30. Returns 201; duplicate employee code returns 409. |
| `GET /employees` | Lists employees, optionally filtered by exact department; sorted by employee code ascending. `page` defaults to 1; `page_size` defaults to 20 and is limited to 100. `total` is the filtered count. |
| `POST /attendance/punch-in` | Creates a day's attendance record for `PRESENT`, `WFH`, or `ON_DUTY`. Optional `punched_at` is epoch milliseconds; omission uses the current time. Returns 201; unknown employee is 404 and a duplicate employee/date record is 409. |
| `POST /attendance/punch-out` | Closes the most recent record whose punch-in is not after the requested timestamp. Computes work hours, overtime, and half-day state. Returns 200; no eligible record is 404, a closed newest record is 409, and invalid elapsed times are 422. |
| `GET /attendance` | Filters by employee, inclusive date bounds, and status; sorted by date descending then employee code ascending. Pagination uses the same defaults and maximum as employee listing. |
| `PATCH /attendance/{emp_code}/{date}` | Corrects status and/or punch times for an existing record. Omitted fields remain unchanged; derived fields are recomputed and a successful correction appends one history entry. Unknown employee or record is 404; a concurrent conflicting update is 409. |
| `GET /analytics/employees/{emp_code}/monthly` | Requires `month=YYYY-MM`; returns working days, present days, leave days, late counts/minutes, overtime minutes, and attendance percentage. Unknown employee is 404. |
| `GET /analytics/departments/summary` | Requires `month=YYYY-MM`; optional exact-match `department` filter. Returns headcount and monthly attendance aggregates by department. |
| `GET /analytics/leaderboard/late` | Requires `month=YYYY-MM`; optional `department`; `limit` defaults to 10 and ranges from 1 to 50. Uses competition ranking and applies the limit after ranking, so ties at the limit can add rows. |
| `GET /analytics/departments/{department}/trend` | Requires inclusive `from` and `to` dates, with a maximum range of 92 days. Returns one row per day, including weekends and days without records. Unknown department is 404. |
| `GET /admin/explain/{endpoint}` | Returns raw MongoDB `executionStats` explain output for one of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, or `department_trend`. Pass the parameters required by the selected endpoint. |

Punch-out and regularization use conditional MongoDB updates against the punch-field snapshot used for their calculations. If another operation changes that snapshot first, the stale request is rejected with the route's existing HTTP 409 conflict response. Retrying after re-reading the record avoids silently overwriting the newer state.

### Calculation conventions

- Shift times and attendance-day assignment use IST (UTC+05:30); stored instants are UTC and truncated to whole seconds.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence; `ABSENT` and `LEAVE` do not.
- A punch is late only when it is more than 10 minutes after shift start. Late and overtime minutes are whole minutes.
- Work hours are rounded to two decimal places using half-up rounding. A rounded duration below 4.50 hours is a half-day; it contributes 0.5 present days.
- Working days are Monday through Friday with no holiday calendar. Monthly present days count weekday records; monthly leave days count all `LEAVE` records in the month.
- Department headcount includes employees who joined by the period end, even if they have no attendance logs, and excludes later joiners.
- Department trend attendance rate is only defined for working days with nonzero headcount. Its seven-row moving average ignores null rates and starts at the requested range boundary.

## Tests

Run the normal suite without MongoDB integration tests:

```bat
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

MongoDB integration tests are explicitly opt-in and use a uniquely named temporary database with an ownership marker. Start a dedicated local test MongoDB instance, then in CMD:

```bat
set MONGO_TEST_URI=mongodb://localhost:27017
set RUN_MONGO_INTEGRATION=1
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

The integration fixture does not use `MONGO_TEST_URI` as the application database name; it creates an isolated
`employee_attendance_test_<uuid>` database and drops only the database whose ownership marker it verifies. If the URI is
unset or the opt-in flag is absent, those tests skip. Use a MongoDB instance reserved for testing.

The normal command above skips the opt-in MongoDB tests. The latest verified normal run reported 48 passed and 11 skipped; with `MONGO_TEST_URI` and `RUN_MONGO_INTEGRATION=1` enabled, the latest integration-enabled run reported 59 passed. Both runs reported two existing FastAPI startup-event deprecation warnings.

## Standalone 100,000-record benchmark

`scripts/benchmark_100k.py` is a separate, explicit benchmark, not part of pytest or application startup. It requires a
MongoDB server dedicated to the benchmark and both environment variables below. The script generates a fresh
`employee_attendance_benchmark_<uuid>` database, inserts exactly 100,000 synthetic attendance records and 1,000 synthetic
employees, checks analytics against independently calculated values, gathers explain evidence, and drops only the marked
database it owns.

Do not point this at production or a database containing data you need. From Windows CMD:

```bat
set MONGO_BENCHMARK_URI=mongodb://localhost:27017
set RUN_100K_BENCHMARK=1
.venv\Scripts\python.exe scripts\benchmark_100k.py
```

The benchmark URI is required explicitly and never falls back to `MONGO_URI` or `MONGO_DB`. Review `scripts/benchmark_100k.py`
before execution for its checks and cleanup behavior. The benchmark has been run against MongoDB 8.2.1 with 1,000 employees
and 100,000 attendance records. The reported department-trend median was approximately 1.483 seconds and the late-leaderboard
median approximately 346 ms. These request times used in-process ASGI requests; they do not represent Uvicorn/network latency
or establish performance on MongoDB 6.0 or a production dataset/environment. See `REVIEW.md` for the recorded results and
limitations.
