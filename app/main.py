"""Employee Attendance & Analytics API."""

import os

import re


from datetime import datetime, date, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal

from bson.json_util import dumps, loads


from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Response

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

from pymongo.errors import DuplicateKeyError

from pymongo.read_concern import ReadConcern



load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)

db = client[MONGO_DB]

employees = db.employees

logs = db.attendance_logs

UTC = timezone.utc

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")

STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")





def now_utc():

    return datetime.now(UTC).replace(microsecond=0)





def ms_to_dt(value: int) -> datetime:

    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:

        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)





def dt_to_ms(value):

    if value is None:

        return None

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return int(value.timestamp()) * 1000





def normalize_dt(value):

    if value is None:

        return None

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC).replace(microsecond=0)





def round_half_up(value, places=2):

    if value is None:

        return None

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))





def valid_date(s):

    try:

        return date.fromisoformat(s).isoformat() == s

    except (ValueError, TypeError):

        return False





def month_bounds(month):

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):

        raise HTTPException(422, "month must be YYYY-MM")

    start = date.fromisoformat(month + "-01")

    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1)

    return start, end





def working_days(start: date, end: date):

    if end < start:

        return 0

    count = 0

    d = start

    while d <= end:

        if d.weekday() < 5:

            count += 1

        d += timedelta(days=1)

    return count





def shift_datetimes(emp, punch_in):

    """Return shift-start and shift-end instants for punch_in's attendance shift."""

    local = punch_in.astimezone(IST)

    start_h, start_m = map(int, emp["shift_start"].split(":"))

    end_h, end_m = map(int, emp["shift_end"].split(":"))

    overnight = (end_h, end_m) <= (start_h, start_m)

    attendance_day = local.date()

    if overnight and (local.hour, local.minute) < (end_h, end_m):

        attendance_day -= timedelta(days=1)

    start_local = datetime.combine(attendance_day, time(start_h, start_m), IST)

    end_day = attendance_day + timedelta(days=1) if overnight else attendance_day

    end_local = datetime.combine(end_day, time(end_h, end_m), IST)

    return start_local.astimezone(UTC), end_local.astimezone(UTC), attendance_day.isoformat()






def compute_late_minutes(punch_in, emp):
    start, _, _ = shift_datetimes(emp, punch_in)

    elapsed_seconds = int((punch_in - start).total_seconds())
    grace_seconds = 10 * 60

    late_seconds = max(0, elapsed_seconds - grace_seconds)
    return late_seconds // 60






def compute_work_hours(punch_in, punch_out):

    seconds = (punch_out - punch_in).total_seconds()

    return round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)





def compute_overtime(punch_out, emp, punch_in):

    _, end, _ = shift_datetimes(emp, punch_in)

    mins = int((punch_out - end).total_seconds()) // 60

    return mins if mins >= 30 else 0





def present_fraction(doc):

    if doc.get("status") not in PRESENCE:

        return 0.0

    return 0.5 if doc.get("half_day", False) else 1.0





def public_employee(doc):

    return {k: v for k, v in doc.items() if k != "_id"}





def public_record(doc):

    out = {k: v for k, v in doc.items() if k != "_id"}

    out["punch_in"] = dt_to_ms(normalize_dt(out.get("punch_in")))

    out["punch_out"] = dt_to_ms(normalize_dt(out.get("punch_out")))

    out["history"] = out.get("history") or []

    for entry in out["history"]:

        entry["at"] = dt_to_ms(normalize_dt(entry.get("at")))

        for change in entry.get("changes", {}).values():

            for side in ("from", "to"):

                if isinstance(change.get(side), datetime):

                    change[side] = dt_to_ms(normalize_dt(change[side]))

    out.setdefault("work_hours", None)

    out.setdefault("late_minutes", 0)

    out.setdefault("overtime_minutes", 0)

    out.setdefault("half_day", False)

    return out





def create_indexes():

    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")

    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined_code")

    employees.create_index([("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="employee_joined_code")

    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")

    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")

    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punchin")

    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_code")





@app.on_event("startup")

def startup():

    create_indexes()





class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: date



    @field_validator("joined_on", mode="before")

    @classmethod

    def date_string(cls, v):

        if isinstance(v, date):

            return v

        if not isinstance(v, str) or not valid_date(v):

            raise ValueError("joined_on must be YYYY-MM-DD")

        return v



    @field_validator("shift_end")

    @classmethod

    def different_shift(cls, v, info):

        if info.data.get("shift_start") == v:

            raise ValueError("shift_start must differ from shift_end")

        return v





class PunchInIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[StrictInt] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



    @field_validator("punched_at")

    @classmethod

    def timestamp(cls, v):

        if v is not None:

            ms_to_dt(v)

        return v





class PunchOutIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[StrictInt] = None



    @field_validator("punched_at")

    @classmethod

    def timestamp(cls, v):

        if v is not None:

            ms_to_dt(v)

        return v





class RegularizeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)



    @field_validator("punch_in", "punch_out")

    @classmethod

    def timestamps(cls, v):

        if v is not None:

            ms_to_dt(v)

        return v





def validate_page(page, page_size):

    if page < 1 or page_size < 1 or page_size > 100:

        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")





@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except Exception:

        raise HTTPException(503, "MongoDB unavailable")





@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee and return a JSON-safe public representation."""
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()

    try:
        result = employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    # Fetch the saved document, then remove MongoDB's ObjectId before returning it.
    created = employees.find_one({"_id": result.inserted_id})
    if created is None:
        raise HTTPException(status_code=500, detail="Employee was inserted but could not be retrieved")

    created["created_at"] = dt_to_ms(normalize_dt(created.get("created_at")))
    return public_employee(created)


@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    q = {"department": department} if department is not None else {}

    total = employees.count_documents(q)

    items = [public_employee(d) for d in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]

    for item in items:

        item["created_at"] = dt_to_ms(normalize_dt(item["created_at"]))

    return {"items": items, "total": total, "page": page, "page_size": page_size}





@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp = employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    _, _, attendance_date = shift_datetimes(emp, ts)

    doc = {

        "emp_code": body.emp_code, "date": attendance_date, "status": body.status,

        "punch_in": ts, "punch_out": None, "work_hours": None,

        "late_minutes": compute_late_minutes(ts, emp), "overtime_minutes": 0,

        "half_day": False, "history": [],

    }

    try:

        logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "already punched in for this date")

    return public_record(doc)





@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp = employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])

    if not record:

        raise HTTPException(404, "no punch-in found")

    pin = normalize_dt(record.get("punch_in"))

    if record.get("punch_out") is not None:

        raise HTTPException(409, "record already punched out")

    if ts <= pin or ts > pin + timedelta(hours=24):

        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    work = compute_work_hours(pin, ts)

    update = {"punch_out": ts, "work_hours": work, "overtime_minutes": compute_overtime(ts, emp, pin), "half_day": work < 4.50}

    updated = logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {"$set": update}, return_document=ReturnDocument.AFTER)

    if not updated:

        raise HTTPException(409, "record already punched out")

    return public_record(updated)





@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,

    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)

):

    q = {}

    if emp_code is not None: q["emp_code"] = emp_code

    if date_from and not valid_date(date_from): raise HTTPException(422, "date_from must be YYYY-MM-DD")

    if date_to and not valid_date(date_to): raise HTTPException(422, "date_to must be YYYY-MM-DD")

    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")

    if date_from or date_to:

        q["date"] = {}

        if date_from: q["date"]["$gte"] = date_from

        if date_to: q["date"]["$lte"] = date_to

    if status is not None:

        if status not in STATUSES: raise HTTPException(422, "invalid status")

        q["status"] = status

    total = logs.count_documents(q)

    docs = logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)

    return {"items": [public_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}





@app.patch("/attendance/{emp_code}/{att_date}")

def regularize_attendance(emp_code: str, att_date: date, body: RegularizeIn):

    emp = employees.find_one({"emp_code": emp_code})

    if not emp: raise HTTPException(404, "employee not found")

    dstr = att_date.isoformat()

    old = logs.find_one({"emp_code": emp_code, "date": dstr})

    if not old: raise HTTPException(404, "attendance record not found")

    old = dict(old)

    current = {k: old.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}

    desired = dict(current)

    payload = body.model_dump(exclude_unset=True)

    for field in ("status", "punch_in", "punch_out"):

        if field in payload:

            desired[field] = payload[field] if field == "status" else (ms_to_dt(payload[field]) if payload[field] is not None else None)

    status = desired["status"]

    if status in ("ABSENT", "LEAVE"):

        if ("punch_in" in payload and payload["punch_in"] is not None) or ("punch_out" in payload and payload["punch_out"] is not None):

            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

        desired["punch_in"] = desired["punch_out"] = None

    else:

        if desired["punch_in"] is None: raise HTTPException(422, "presence status requires punch_in")

        _, _, actual_date = shift_datetimes(emp, normalize_dt(desired["punch_in"]))

        if actual_date != dstr: raise HTTPException(422, "punch_in must belong to the attendance date")

        if desired["punch_out"] is not None:

            if desired["punch_out"] <= desired["punch_in"] or desired["punch_out"] > desired["punch_in"] + timedelta(hours=24):

                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if status in ("ABSENT", "LEAVE"):

        desired.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)

    else:

        pin = normalize_dt(desired["punch_in"])

        desired["late_minutes"] = compute_late_minutes(pin, emp)

        if desired["punch_out"] is None:

            desired.update(work_hours=None, overtime_minutes=0, half_day=False)

        else:

            pout = normalize_dt(desired["punch_out"])

            work = compute_work_hours(pin, pout)

            desired.update(work_hours=work, overtime_minutes=compute_overtime(pout, emp, pin), half_day=work < 4.50)

    changes = {}

    for field, newval in desired.items():

        oldval = current.get(field)

        if isinstance(oldval, datetime): oldval = normalize_dt(oldval)

        if isinstance(newval, datetime): newval = normalize_dt(newval)

        if oldval != newval:

            changes[field] = {"from": current.get(field), "to": newval}

    if not changes: raise HTTPException(422, "request makes no changes")

    history_change = {}

    for field, pair in changes.items():

        history_change[field] = pair

    history = old.get("history") or []

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": history_change}

    # Compare-and-swap prevents two edits from overwriting one another's audit trail.

    filter_doc = {"_id": old["_id"]}

    filter_doc["history"] = old.get("history", []) if "history" in old else {"$exists": False}

    update_doc = {k: v for k, v in desired.items() if k in changes}

    # Also persist derived defaults/values when recomputation changes them.

    update_doc.update({k: desired[k] for k in ("work_hours", "late_minutes", "overtime_minutes", "half_day")})

    updated = logs.find_one_and_update(filter_doc, {"$set": update_doc, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)

    if not updated: raise HTTPException(409, "record changed concurrently; retry")

    return public_record(updated)





def monthly_log_pipeline(emp_code, month):

    start, end = month_bounds(month)

    return [

        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},

        {"$project": {"date": 1, "status": 1, "half_day": {"$ifNull": ["$half_day", False]},

                      "is_weekday": {"$and": [
                          {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                          {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
                      ]},
                      "late_minutes": {"$ifNull": ["$late_minutes", 0]},

                      "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}}},

        {"$group": {"_id": None,

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, "$is_weekday"]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},

            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": "$late_minutes"},

            "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}

    ]





@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})

    if not emp: raise HTTPException(404, "employee not found")

    start, end = month_bounds(month)

    join_date = date.fromisoformat(emp["joined_on"])

    work_start = max(start, join_date)

    wd = working_days(work_start, end)

    rows = list(logs.aggregate(monthly_log_pipeline(emp_code, month)))

    agg = rows[0] if rows else {}

    present = round_half_up(agg.get("present_days", 0), 2)

    pct = round_half_up(present / wd * 100, 4) if wd else None

    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,

        "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),

        "total_late_minutes": agg.get("total_late_minutes", 0), "total_overtime_minutes": agg.get("total_overtime_minutes", 0),

        "attendance_pct": pct}





def department_summary_pipeline(month, department=None):

    start, end = month_bounds(month)

    match = {"joined_on": {"$lte": end.isoformat()}}

    if department is not None: match["department"] = department

    return [

        {"$match": match},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},

            "pipeline": [{"$match": {"$expr": {"$and": [

                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}

            ]}}}], "as": "month_logs"}},

        {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},

        {"$group": {"_id": "$department",

            "headcount_codes": {"$addToSet": "$emp_code"},

            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$month_logs.status", list(PRESENCE)]},
                {"$ne": ["$month_logs.date", None]},
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$month_logs.date"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$month_logs.date"}}}, 6]}
            ]}, {"$cond": [{"$ifNull": ["$month_logs.half_day", False]}, 0.5, 1]}, 0]}},

            "work_hours_sum": {"$sum": {"$cond": [{"$and": [

                {"$in": ["$month_logs.status", list(PRESENCE)]}, {"$ne": ["$month_logs.work_hours", None]}

            ]}, "$month_logs.work_hours", 0]}},

            "work_hours_count": {"$sum": {"$cond": [{"$and": [

                {"$in": ["$month_logs.status", list(PRESENCE)]}, {"$ne": ["$month_logs.work_hours", None]}

            ]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},

            "leave_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}},

            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]}}

        }},

        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},

            "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},

                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},

            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},

        {"$sort": {"department": 1}}

    ]





@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):

    rows = list(employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))

    start, end = month_bounds(month)

    # Filter departments with headcount zero (normally excluded by joined_on match).

    items = []

    for row in rows:

        if row["headcount"] == 0: continue

        row["present_days"] = round_half_up(row.get("present_days", 0), 2)

        row["avg_work_hours"] = round_half_up(row.get("avg_work_hours"), 2)

        items.append(row)

    return {"month": month, "items": items}





def leaderboard_pipeline(month, department=None):

    start, end = month_bounds(month)

    pipeline = [

        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},

        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

    ]

    if department is not None:

        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": 50}}},

        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},

        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}

    ]

    return pipeline





@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    pipeline = leaderboard_pipeline(month, department)

    pipeline[-2] = {"$match": {"rank": {"$lte": limit}}}

    items = list(logs.aggregate(pipeline, allowDiskUse=True))

    return {"month": month, "items": items}





def trend_pipeline(department, start, end):

    # Seed from an existing department employee so $densify emits rows even

    # when the attendance collection has no documents in the requested range.

    begin_dt = datetime.combine(start, time.min, UTC)

    end_dt = datetime.combine(end, time.min, UTC)

    return [

        {"$match": {"department": department}},

        {"$limit": 1},

        {"$project": {"_id": 0, "day": {"$literal": begin_dt}}},

        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [begin_dt, end_dt + timedelta(days=1)]}}},

        {"$match": {"day": {"$lt": end_dt + timedelta(days=1)}}},

        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},

        {"$lookup": {"from": "attendance_logs", "let": {"daystr": "$date"}, "pipeline": [

            {"$match": {"$expr": {"$eq": ["$date", "$$daystr"]}}},

            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

            {"$unwind": "$employee"},

            {"$match": {"$expr": {"$and": [

                {"$eq": ["$employee.department", department]}

            ]}}},

            {"$project": {"status": 1, "half_day": {"$ifNull": ["$half_day", False]}, "late_minutes": {"$ifNull": ["$late_minutes", 0]}}}

        ], "as": "daily_logs"}},

        {"$lookup": {"from": "employees", "let": {"daystr": "$date"}, "pipeline": [

            {"$match": {"$expr": {"$and": [

                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$daystr"]}

            ]}}},

            {"$count": "n"}

        ], "as": "hc"}},

        {"$set": {

            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},

            "present_count": {"$sum": {"$map": {"input": "$daily_logs", "as": "l",

                "in": {"$cond": [{"$in": ["$$l.status", list(PRESENCE)]},

                    {"$cond": ["$$l.half_day", 0.5, 1]}, 0]}}}},

            "late_count": {"$size": {"$filter": {"input": "$daily_logs", "as": "l", "cond": {"$gt": ["$$l.late_minutes", 0]}}}},

            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}

        }},

        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},

            {"$divide": ["$present_count", "$headcount"]}, None]}}},

        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {

            "$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},

        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,

            "attendance_rate": 1, "moving_avg_7d": 1}},

        {"$sort": {"date": 1}}

    ]



@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):

    if not valid_date(from_date) or not valid_date(to_date): raise HTTPException(422, "from and to must be YYYY-MM-DD")

    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)

    if end < start: raise HTTPException(422, "to must be on or after from")

    if (end-start).days + 1 > 92: raise HTTPException(422, "range cannot exceed 92 days")

    if not employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")

    pipeline = trend_pipeline(department, start, end)

    items = list(employees.aggregate(pipeline, allowDiskUse=True))

    # Correct weekday flag: MongoDB dayOfWeek is 1=Sunday, 7=Saturday.

    for item in items:

        item["is_working_day"] = date.fromisoformat(item["date"]).weekday() < 5

        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)

        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)

    return {"department": department, "items": items}





@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q = {}

        if emp_code is not None:
            q["emp_code"] = emp_code

        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        if date_from and not valid_date(date_from):
            raise HTTPException(422, "date_from must be YYYY-MM-DD")
        if date_to and not valid_date(date_to):
            raise HTTPException(422, "date_to must be YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in STATUSES:
            raise HTTPException(422, "invalid status")

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return Response(
            content=dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": monthly_log_pipeline(emp_code, month),
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return Response(
            content=dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": department_summary_pipeline(month, department),
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return Response(
            content=dumps({
                "endpoint": endpoint,
                "collection": "employees",
                "explain": explain,
            }),
            media_type="application/json",
        )

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        pipeline = leaderboard_pipeline(month, department)
        pipeline[-2] = {"$match": {"rank": {"$lte": limit}}}

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return Response(
            content=dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )

        if not valid_date(from_date) or not valid_date(to_date):
            raise HTTPException(422, "invalid date")

        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)

        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")

        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": trend_pipeline(department, start, end),
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return Response(
            content=dumps({
                "endpoint": endpoint,
                "collection": "employees",
                "explain": explain,
            }),
            media_type="application/json",
        )

    raise HTTPException(422, "unknown endpoint")