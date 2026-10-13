# Implementation decisions

This file records behavior and persistence choices visible in the current implementation and OpenAPI contract. It does not
assert undocumented design history. The supplied `openapi.yaml` remains the source of truth.

## Data identity and timestamps

- `emp_code` is the employee's client-supplied identity. A unique employee index enforces it.
- Attendance records are unique by `(emp_code, date)`, enforced by a compound unique index.
- Attendance `date` values use the `YYYY-MM-DD` attendance day. Punch instants are stored as UTC datetimes; shift and
  attendance-day calculations use IST (UTC+05:30).
- Punch timestamps accepted by the request models are strict integer epoch milliseconds from `100000000000` through
  `4102444800000`, inclusive. If the optional timestamp is omitted, punch-in/punch-out uses the current time.
- Stored and returned punch times are truncated to whole seconds. Employee creation accepts `joined_on` as a strict
  `YYYY-MM-DD` date string.

## Attendance calculations

- `PRESENT`, `WFH`, and `ON_DUTY` are presence statuses. `ABSENT` and `LEAVE` are non-presence statuses.
- A punch is late only when it is strictly more than ten minutes after shift start; `late_minutes` is whole elapsed minutes
  from shift start.
- Overtime is whole minutes after shift end, and is recorded only when the overrun reaches 30 minutes.
- `work_hours` is elapsed seconds divided by 3,600, rounded to two decimal places with half-up rounding. `half_day` is
  true when the rounded value is less than 4.50 hours.
- Punch-out selects the most recent eligible record with `punch_in` at or before the requested instant. It checks whether
  that selected record is closed and does not fall back to an older open record. Its conditional update prevents a
  concurrent punch-out from overwriting a close.
- Regularization recomputes derived values and appends an audit-history entry containing the regularizer, reason, time,
  and only fields that actually changed. Concurrent regularization uses a conditional update to detect conflicting edits.

## MongoDB indexes

Startup creates these indexes idempotently:

| Collection | Index | Purpose/constraint |
|---|---|---|
| `employees` | `uq_employees_emp_code` on `emp_code` | Unique employee identity and employee lookups. |
| `employees` | `ix_employees_department_joined` on `(department, joined_on)` | Department filtering and joined-date eligibility. |
| `attendance_logs` | `uq_attendance_emp_date` on `(emp_code, date)` | One attendance record per employee per attendance day. |
| `attendance_logs` | `ix_attendance_date_emp` on `(date DESC, emp_code ASC)` | Date-oriented attendance access and sorting. |
| `attendance_logs` | `ix_attendance_status_date_emp` on `(status, date DESC, emp_code ASC)` | Status/date filtered attendance listing. |

## Analytics and explain behavior

- Monthly and department analytics are calculated from MongoDB aggregation pipelines. Monthly working days are weekdays
  beginning no earlier than `joined_on`; monthly present days count weekday presence records, while `leave_days` counts all
  monthly `LEAVE` records. Attendance percentages use half-up rounding to four decimal places; other reported numeric
  calculations follow the contract's two-decimal half-up rule where applicable.
- Department summary headcount includes employees whose joining date is on or before month end, including employees with
  no attendance records; employees who join later are excluded. Average work hours is over qualifying present-status
  records with work-hour values.
- The late leaderboard ranks by total lateness using competition ranking, sorts ties by employee code, and applies its
  limit to rank values after ranking. Department filtering is applied before ranking.
- Department trend returns one row for every requested calendar date. Headcount is date-sensitive; attendance rate is
  nullable for weekends or zero headcount, and the seven-row moving average excludes null rates.
- `/admin/explain/{endpoint}` exposes MongoDB `executionStats` for the named endpoint's query or aggregation. The trend
  aggregation starts at the database level, so its explain wrapper identifies the collection as `database`.
