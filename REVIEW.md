# Review of the starter `app/main.py`

| # | Where | What is wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | `health` | Always returns 200 without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | `create_employee` | A read-before-insert duplicate check races under concurrent requests; it also returns a naive `datetime` instead of epoch milliseconds. | Send parallel creates with the same `emp_code`; inspect response type. | Unique index on `emp_code`, handle `DuplicateKeyError`, store UTC time and serialize it as milliseconds. |
| 3 | `list_employees` | `skip = page * page_size` skips page 1; `total` ignores the department filter; results are not sorted. | Compare page 1 with page 2 and filter by a department. | Use `(page - 1) * page_size`, filtered count, and `emp_code` sort. |
| 4 | `compute_late_minutes` | Replaces the punch-in date with the shift start time, so it mishandles time zones, overnight shifts, and date boundaries. | Test a punch just after midnight for a night shift. | Derive the actual shift start in IST and compare UTC instants; apply the strict 10-minute threshold and floor elapsed minutes. |
| 5 | `compute_work_hours` | Python's built-in `round` does not guarantee the contract's decimal half-up rule. | Test a duration at a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | Assumes the shift end is on the same date and does not enforce the 30-minute minimum. | Test an overnight shift and 29 versus 30 minutes after shift end. | Resolve the correct shift end for the punch-in and count only when elapsed overtime is at least 30 whole minutes. |
| 7 | `EmployeeIn` / `PunchInIn` | The starter does not validate employee fields, status values, or epoch-millisecond timestamps. | Submit malformed email, status, or a string/float timestamp. | Add Pydantic constraints and strict integer timestamp validation. |
| 8 | `punch_in` | Does not return 404 for an unknown employee, uses local naive timestamps, derives the date from local time rather than IST, and uses a race-prone duplicate lookup. | Unknown employee, near-midnight timestamp, or parallel punch-ins. | Convert/validate UTC timestamps, derive the IST shift date, and rely on a unique `(emp_code, date)` index for race safety. |
| 9 | `list_attendance` | Loads all matching records into Python, so memory and latency grow with the collection; it returns MongoDB IDs and does not reliably validate pagination. | Run against a large collection and inspect response fields. | Filter, count, sort, skip, and limit in MongoDB; exclude `_id`; validate page inputs. |
| 10 | Remaining contract endpoints | Punch-out, regularization, analytics, and explain endpoints are absent. | Check the OpenAPI paths or call each endpoint. | Implement all required routes, aggregation pipelines, audit history, and execution-stats explain responses. |
| 11 | Monthly analytics | Presence totals must ignore weekend records, but late/overtime totals must still include them. | Seed a present weekend record and compare monthly results. | Check weekday status inside the MongoDB aggregation for `present_days`; retain all records for late/overtime totals. |
| 12 | Explain endpoint | Returning a plan alone does not meet the response schema; parameters also need the same validation and query construction as the endpoint being explained. | Inspect `/admin/explain/...` response and try invalid filters. | Return `{endpoint, collection, explain}` and validate filters/ranges before running executionStats explain. |

## Checked and retained

- The unique employee business key is `emp_code`; MongoDB `_id` is internal only.
- A unique compound attendance index on `(emp_code, date)` is appropriate because the data model allows at most one daily record per employee.
- Manual corrections append audit entries; punch-in and punch-out do not write history.
- Missing `history` and `half_day` values in seeded documents are treated as an empty list and `false` respectively.
