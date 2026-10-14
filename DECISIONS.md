# Design decisions

1. **Indexes.** I create a unique index on employee `emp_code`, a unique compound index on attendance `(emp_code, date)`, and supporting indexes for department/join-date filtering, date-based attendance listing, employee punch lookup, and date/status filtering. The unique indexes enforce business rules even when requests race. I avoid indexing every field because extra indexes slow writes and consume storage.

2. **Punch-in race.** Both requests may read the employee, but both attempt an insert under the same `(emp_code, date)` key. MongoDB's unique index allows one insert only. The winner returns 201; the duplicate-key loser returns 409. The pre-check is not relied on for correctness.

3. **Ties.** The leaderboard uses `$setWindowFields` with `$rank`, ordered by total late minutes. It filters `rank <= limit` after ranking, then sorts tied rows by `emp_code`; a tie at the cutoff is fully included and the next rank is skipped.

4. **Headcount.** The department summary starts from employees joined on or before the period end, then looks up that month's logs with `preserveNullAndEmptyArrays`. Employees without logs remain in the group and count toward headcount.

5. **100x data.** I would measure query plans and latency, add production monitoring, and consider pre-aggregated daily summaries or a reporting store for expensive cross-collection analytics. I would preserve the source logs as the audit-grade record and validate any materialized results against them.
