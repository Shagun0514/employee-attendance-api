# Starter-code review and implementation verification

## Review of the supplied starter `app/main.py`

The observations below refer to `candidate_kit/app/main.py` as supplied. The symptom column gives a concrete way each
problem can be observed; it does not claim that every example was separately run against the starter. Fix references are
to the current implementation in this repository.

| # | Starter location | Defect and observable symptom | Current correction |
|---|---|---|---|
| 1 | `health()` (starter line 73) | Always returns `{"status":"ok"}` without contacting MongoDB, so it reports readiness while the database is unavailable. | The current handler pings MongoDB and returns 503 on a database error. |
| 2 | `EmployeeIn` (starter lines 53–60) | Accepts arbitrary employee codes, email strings, shift formats, and date strings; it also accepts equal shift times. Invalid examples can be created instead of returning 422. | `EmployeeIn` validates the contract's formats, lengths, date string, and differing shift times. |
| 3 | `create_employee()` (starter lines 78–86) | Duplicate detection is check-then-insert without a unique database constraint. Two concurrent creates can both pass the check. | Startup creates unique `uq_employees_emp_code`; duplicate-key errors become 409. |
| 4 | `create_employee()` (starter line 82) | Uses a naive `datetime.now()` and returns the datetime through JSON, rather than the contract's epoch-millisecond `created_at`. | The current code stores UTC and serializes the public value as epoch milliseconds. |
| 5 | `list_employees()` (starter lines 89–94) | `skip = page * page_size` skips the first page when `page=1`. | The current offset is `(page - 1) * page_size`. |
| 6 | `list_employees()` (starter lines 92–96) | `total` counts the whole collection rather than the optional department-filtered set; items are not sorted by `emp_code`. | The current count uses the applied filter and results sort by employee code ascending. |
| 7 | `list_employees()` (starter signature) | Page and page-size bounds are not validated. Values such as page 0 or page size above 100 are accepted. | The current query parameters enforce page >= 1 and page size 1–100. |
| 8 | `compute_late_minutes()` (starter lines 33–38) | The grace period is checked after truncating elapsed minutes. For a 09:30 shift, 09:40:01 truncates to 10 and is incorrectly treated as not late, though the contract says any instant strictly after 09:40 is late. | `calculate_late_minutes()` compares elapsed seconds to 600 first, then floors minutes. |
| 9 | `compute_late_minutes()` and punch-in date calculation (starter lines 33–38, 102–103) | The helper replaces clock fields on the punch timestamp without converting to IST or assigning the previous date for an overnight shift. UTC/local boundary and overnight examples can get the wrong attendance day or lateness. | The current code converts to IST, assigns the attendance date using the overnight rule, then computes lateness against that date. |
| 10 | `compute_work_hours()` (starter lines 40–41) | Python's built-in `round()` uses ties-to-even, not the required half-up rule. A 4.125-hour duration can round to 4.12 rather than 4.13. | `round_half_up()` implements the specified decimal rounding. |
| 11 | `compute_overtime()` (starter lines 44–50) | Counts any positive overrun as overtime, although the contract requires at least 30 minutes, and always places shift end on the attendance date. A 20-minute overrun is incorrectly counted; an overnight shift uses the wrong end instant. | Punch-out and regularization apply the 30-minute threshold and advance the end date for overnight shifts. |
| 12 | `punch_in()` (starter lines 100–122) | Does not handle an unknown employee before indexing `emp`, so an unknown code can produce a server error instead of 404. It also accepts arbitrary status values. | The current handler checks employee existence and restricts status to presence statuses. |
| 13 | `PunchInIn` and `punch_in()` (starter lines 63–70, 102) | Timestamp coercion/range are not constrained; explicit null and falsey values such as zero are treated as omission, and `datetime.fromtimestamp()` uses the host timezone. Invalid input can be accepted or interpreted as the wrong instant. | The request model enforces strict integer epoch-millisecond bounds and rejects explicit null; conversion is UTC-aware and attendance-day calculation uses IST. |
| 14 | `punch_in()` (starter lines 104–124) | A pre-insert existence check is not race-safe. Parallel punch-ins can both observe no record and insert duplicates. | Unique `(emp_code, date)` index `uq_attendance_emp_date` arbitrates the race; duplicate-key errors return 409. |
| 15 | `list_attendance()` (starter lines 125–152) | Loads all matching records, sorts and paginates in Python, which grows memory and work with the collection instead of applying bounded pagination in MongoDB. | The current endpoint performs count, sort, skip, and limit in MongoDB. |
| 16 | `list_attendance()` (starter lines 125–152) | Does not validate date formats/order, status values, or page bounds; it sorts only by date (no employee-code tie-breaker) and adds an `id` field that is absent from the response contract. | The current handler validates filters and pagination, sorts by date descending then employee code ascending, and omits MongoDB identity fields. |

The starter ends with a TODO list for punch-out, regularization, analytics, and explain routes (starter lines 154–163). Those
were explicitly assigned as Part B implementation work, rather than defects in a supplied handler; all listed routes now
exist in `app/main.py`.

### Behavior inspected and retained

- The starter's attendance date range operators are inclusive (`$gte` and `$lte`); the current implementation preserves
  inclusive bounds and adds strict date parsing and reversed-range validation.
- Its late-minute definition intends to count whole minutes from shift start, not from the end of the grace period. The
  current implementation preserves that definition while correcting the strict 10-minute boundary.
- The starter's defaults of page 1 and page size 20 are the contract defaults. The current implementation retains them and
  adds the required validation and maximum page size.

## Cross-route concurrency review

The punch-out and regularization handlers both calculate updates from an attendance document they read first. Before the
hardening change, punch-out guarded only the record ID and open `punch_out`, while regularization guarded the record ID and
history array. Since punch-out does not append history, a regularization based on an earlier open-record snapshot could
overwrite derived fields after punch-out closed the record. In the reverse ordering, punch-out could commit values
calculated from an old `punch_in` after regularization changed that punch time.

The current punch-out compare-and-set filter checks `_id`, the snapshotted `punch_in`, and `punch_out: null`. The
regularization filter retains the `_id` and exact history-array guard and also checks the snapshotted `status`, `punch_in`,
and `punch_out`. A filter mismatch produces the existing HTTP 409 conflict response for that route; successful corrections
continue to append one history entry atomically with their field updates.

Two deterministic MongoDB integration cases in `tests/test_contract_fixes.py` force both orderings: regularization reads
first and punch-out commits before its update, and punch-out reads first and regularization commits before its update. They
verify the stale request's 409 response and persisted punch values, derived fields, and history. These controlled
interleavings are regression tests, not load testing or a measure of contention under simultaneous production traffic.

## Tests and benchmark

- The normal suite was run with `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`: **48 passed, 9 skipped,
  11 skipped, 0 failed**, with two warnings. The skipped cases are explicitly opt-in MongoDB integration tests; MongoDB
  integration was not enabled for this run.
- With `MONGO_TEST_URI=mongodb://localhost:27017/employee_attendance_test_runner` and
  `RUN_MONGO_INTEGRATION=1`, the integration-enabled full suite reported **59 passed, 0 failed, 0 skipped**, with two
  warnings. This includes both deterministic cross-route stale-write cases.
- MongoDB integration tests use `MONGO_TEST_URI` and `RUN_MONGO_INTEGRATION=1`, with a unique temporary database and an
  ownership marker.
- The standalone benchmark completed against **MongoDB 8.2.1**, using **1,000 synthetic employees** and **exactly 100,000
  synthetic attendance records**. Analytics assertions passed and cleanup of its owned temporary database was verified.
- The department-trend median was approximately **1.483 seconds** and the late-leaderboard median approximately **346 ms**.
  Request measurements were **in-process ASGI latency**, not Uvicorn or production network latency. This run does not
  establish MongoDB 6.0 performance or production workload performance.
- The two benchmark medians are local observations, not service-level guarantees; the input data was synthetic.
- FastAPI reports two existing deprecation warnings for `@app.on_event("startup")`. They do not fail the test suite.

## Operational safety

The benchmark is standalone and opt-in. It requires `MONGO_BENCHMARK_URI` and `RUN_100K_BENCHMARK=1`, creates a fresh
benchmark database, verifies its ownership marker before cleanup, and does not use `MONGO_URI` or `MONGO_DB` to select its
data database. The benchmark must only be run against a MongoDB server where creation and removal of that temporary
database are authorized. Neither the benchmark nor integration tests are part of API startup.
