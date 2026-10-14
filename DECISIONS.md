# Design decisions

1. **Indexes.** Unique indexes `uq_employees_emp_code` and `uq_attendance_emp_date` enforce employee and employee/day
   uniqueness; the latter resolves punch-in races. The department/joined index and two attendance indexes support their
   filter patterns. A department-only index is omitted because department is already the compound employee index prefix;
   a duplicate would add write and storage cost without a new access path.
2. **Punch-in race.** Both requests may pass the employee lookup, but the unique `(emp_code, date)` index permits only one
   insert. The loser receives `DuplicateKeyError`, which the route maps to HTTP 409; no check-then-insert guarantee is
   assumed.
3. **Punch-out and regularization concurrency.** Both operations calculate updates from a previously read attendance
   snapshot, so their final MongoDB writes use compare-and-set filters. Punch-out checks `_id`, the snapshot `punch_in`, and
   that `punch_out` remains null. Regularization checks `_id`, snapshot `status`, `punch_in`, and `punch_out`, while
   preserving its exact history-array guard and appending the audit entry in the same update. A stale snapshot loses with
   the route's existing HTTP 409 response instead of overwriting newer state. This favors consistency over automatically
   retrying a request whose intended correction may need review against the newer record.
4. **Ties.** The leaderboard uses competition rank before filtering by `rank <= limit`. Employees tied at the cutoff are
   all returned with the same rank; output order breaks ties by `emp_code`.
5. **Headcount.** Department summary starts from employees, filters out post-period joiners, then looks up attendance while
   preserving employees with no matching logs. Headcount therefore does not depend on a log existing.
6. **At 100x volume.** I would load-test representative data, then consider maintained monthly aggregates for repeated
   analytics reads. They may reduce repeated scans and joins, but add write and correction consistency work; measurements
   should justify that trade-off first.
