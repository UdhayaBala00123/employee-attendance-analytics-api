# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne attendance assignment.

## Run locally

1. Use Python 3.11 or newer and MongoDB 6.0+.
2. Install dependencies: `pip install -r requirements.txt`.
3. Set `MONGO_URI` and `MONGO_DB` in your environment (a local `.env` may be used for development; do not commit it).
4. From the repository root, run: `uvicorn app.main:app --port 8000`.
5. Check `GET /health` and browse `/docs`.

Indexes are created idempotently at startup. Timestamps at the API boundary are integer epoch milliseconds; MongoDB stores instants as UTC BSON datetimes. Calendar dates and shift times follow the contract's string formats.

See `REVIEW.md` for the starter-code review and `DECISIONS.md` for implementation choices. Run your own tests against MongoDB before submission, especially the hidden-data edge cases: weekend records, overnight shifts, concurrent writes, ties at the leaderboard cutoff, and employees with no attendance logs.
