
"""
Employee Attendance & Analytics API
Run from the repository root:
    uvicorn app.main:app --port 8000

Environment variables:
    MONGO_URI
    MONGO_DB
"""

import os
import re
import json
import calendar
import base64

from bson import json_util
from bson.timestamp import Timestamp
from bson.objectid import ObjectId
from bson.decimal128 import Decimal128
from bson.binary import Binary

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="employee_code_unique",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employee_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING)],
        name="attendance_date_late",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def validate_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        fail422(f"{field} must be a valid YYYY-MM-DD date")


def validate_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        fail422("month must use YYYY-MM format")

    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last_day)


def round_half_up(value, places=2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None

    if (
        type(value) is not int
        or not 100_000_000_000 <= value <= 4_102_444_800_000
    ):
        fail422("Timestamp must be an integer in epoch milliseconds")

    return truncate_seconds(
        datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(IST)
    )


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def shift_datetime(day: str, shift_time: str) -> datetime:
    return datetime.fromisoformat(
        f"{day}T{shift_time}:00"
    ).replace(tzinfo=IST)


def shift_start_for_punch(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> datetime:
    local = to_ist(punch_in)
    start = shift_datetime(local.date().isoformat(), shift_start)

    start_mins = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_mins = int(shift_end[:2]) * 60 + int(shift_end[3:])

    if end_mins <= start_mins and local.strftime("%H:%M") < shift_end:
        start -= timedelta(days=1)

    return start


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str = "18:30",
) -> int:
    start = shift_start_for_punch(
        punch_in,
        shift_start,
        shift_end,
    )
    seconds = (to_ist(punch_in) - start).total_seconds()

    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (
        to_ist(punch_out) - to_ist(punch_in)
    ).total_seconds()

    return round_half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    start_mins = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_mins = int(shift_end[:2]) * 60 + int(shift_end[3:])

    end = shift_datetime(attendance_date, shift_end)

    if end_mins <= start_mins:
        end += timedelta(days=1)

    minutes = int(
        (to_ist(punch_out) - end).total_seconds() // 60
    )

    return minutes if minutes >= 30 else 0


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0

    count = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)

    return count


def employee_or_404(emp_code: str):
    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found")

    return employee


def attendance_response(doc: dict) -> dict:
    """Convert MongoDB BSON dates into API epoch milliseconds."""
    result = dict(doc)
    result.pop("_id", None)
    result.pop("id", None)

    for key in ("punch_in", "punch_out"):
        result[key] = epoch_ms(result.get(key))

    result["history"] = result.get("history") or []

    for entry in result["history"]:
        entry["at"] = epoch_ms(entry.get("at"))

        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = epoch_ms(change.get("from"))
                change["to"] = epoch_ms(change.get("to"))

    result.setdefault("half_day", False)

    return result


def employee_response(doc: dict) -> dict:
    result = dict(doc)
    result.pop("_id", None)
    result["created_at"] = epoch_ms(result.get("created_at"))
    return result


def require_date_range(
    date_from: Optional[str],
    date_to: Optional[str],
):
    start = validate_date(date_from, "date_from") if date_from else None
    end = validate_date(date_to, "date_to") if date_to else None

    if start and end and start > end:
        fail422("date_from must be on or before date_to")


def month_match(month: str) -> dict:
    first, last = validate_month(month)

    return {
        "$gte": first.isoformat(),
        "$lte": last.isoformat(),
    }


def make_json_safe(value):
    """Recursively convert MongoDB BSON values to JSON-safe values."""

    if isinstance(value, Timestamp):
        return {
            "time": value.time,
            "increment": value.inc,
        }

    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, Decimal128):
        return float(value.to_decimal())

    if isinstance(value, Binary):
        return base64.b64encode(bytes(value)).decode("ascii")

    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, dict):
        return {
            str(key): make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def safe_pipeline_explain(collection, pipeline):
    result = db.command({
        "explain": {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    })

    return make_json_safe(result)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        validate_date(value, "joined_on")
        return value

    @field_validator("shift_end")
    @classmethod
    def different_shift_times(cls, value, info):
        start = info.data.get("shift_start")

        if start == value:
            raise ValueError("shift_start and shift_end must differ")

        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in PRESENCE:
            raise ValueError(
                "status must be PRESENT, WFH, or ON_DUTY"
            )
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value


# ---------------------------------------------------------------------------
# Common / employee endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}

    total = db.employees.count_documents(query)

    docs = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [employee_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)

    timestamp = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(datetime.now(IST))
    )

    start_mins = (
        int(employee["shift_start"][:2]) * 60
        + int(employee["shift_start"][3:])
    )
    end_mins = (
        int(employee["shift_end"][:2]) * 60
        + int(employee["shift_end"][3:])
    )

    attendance_day = timestamp.date()

    if (
        end_mins <= start_mins
        and timestamp.strftime("%H:%M") < employee["shift_end"]
    ):
        attendance_day -= timedelta(days=1)

    day_text = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": day_text,
        "status": body.status,
        "punch_in": timestamp.astimezone(UTC),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            timestamp,
            employee["shift_start"],
            employee["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Already punched in for this attendance date",
        )

    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)

    timestamp = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(datetime.now(IST))
    )
    timestamp_utc = timestamp.astimezone(UTC)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": timestamp_utc},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "This record is already punched out")

    punch_in_dt = to_ist(record["punch_in"])
    seconds = (timestamp - punch_in_dt).total_seconds()

    if seconds <= 0 or seconds > 24 * 3600:
        fail422("punch-out must be after punch-in and within 24 hours")

    hours = compute_work_hours(punch_in_dt, timestamp)

    overtime = compute_overtime(
        timestamp,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )

    update = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": timestamp_utc,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.50,
            }
        },
    )

    if update.modified_count != 1:
        raise HTTPException(409, "This record was already punched out")

    record.update({
        "punch_out": timestamp_utc,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    })

    return attendance_response(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    require_date_range(date_from, date_to)

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        if status not in STATUSES:
            fail422("Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    docs = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_text}")
def regularize_attendance(
    emp_code: str,
    date_text: str,
    body: RegularizeIn,
):
    validate_date(date_text)

    employee = employee_or_404(emp_code)

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date_text,
    })

    if not record:
        raise HTTPException(404, "Attendance record not found")

    old = dict(record)

    status = (
        body.status
        if body.status is not None
        else record["status"]
    )

    punch_in_dt = (
        from_epoch_ms(body.punch_in)
        if body.punch_in is not None
        else (
            to_ist(record["punch_in"])
            if record.get("punch_in")
            else None
        )
    )

    punch_out_dt = (
        from_epoch_ms(body.punch_out)
        if body.punch_out is not None
        else (
            to_ist(record["punch_out"])
            if record.get("punch_out")
            else None
        )
    )

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            fail422("ABSENT and LEAVE cannot have punch times")

        punch_in_dt = punch_out_dt = None

    elif punch_in_dt is None:
        fail422("A presence status requires punch_in")

    if punch_in_dt and punch_in_dt.date().isoformat() != date_text:
        overnight = employee["shift_end"] <= employee["shift_start"]
        expected_day = punch_in_dt.date()

        if overnight and punch_in_dt.strftime("%H:%M") < employee["shift_end"]:
            expected_day -= timedelta(days=1)

        if expected_day.isoformat() != date_text:
            fail422("punch_in does not belong to this attendance date")

    if punch_in_dt and punch_out_dt:
        elapsed = (punch_out_dt - punch_in_dt).total_seconds()

        if elapsed <= 0 or elapsed > 24 * 3600:
            fail422("punch_out must be after punch_in and within 24 hours")

    elif punch_out_dt and not punch_in_dt:
        fail422("punch_out requires punch_in")

    new = dict(record)

    new["status"] = status
    new["punch_in"] = (
        punch_in_dt.astimezone(UTC) if punch_in_dt else None
    )
    new["punch_out"] = (
        punch_out_dt.astimezone(UTC) if punch_out_dt else None
    )

    if punch_in_dt:
        new["late_minutes"] = compute_late_minutes(
            punch_in_dt,
            employee["shift_start"],
            employee["shift_end"],
        )
    else:
        new["late_minutes"] = 0

    if punch_in_dt and punch_out_dt:
        new["work_hours"] = compute_work_hours(
            punch_in_dt,
            punch_out_dt,
        )

        new["overtime_minutes"] = compute_overtime(
            punch_out_dt,
            employee["shift_start"],
            employee["shift_end"],
            date_text,
        )

        new["half_day"] = new["work_hours"] < 4.50

    else:
        new["work_hours"] = None
        new["overtime_minutes"] = 0
        new["half_day"] = False

    fields = [
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ]

    changes = {}

    for field in fields:
        before, after = old.get(field), new.get(field)

        if field in ("punch_in", "punch_out"):
            before = epoch_ms(before) if before else None
            after = epoch_ms(after) if after else None

        if before != after:
            changes[field] = {
                "from": before,
                "to": after,
            }

    if not changes:
        fail422("The request does not change the record")

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": {
                key: new[key]
                for key in fields
            },
            "$push": {
                "history": history_entry,
            },
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Concurrent correction; reload and retry",
        )

    new["history"] = (
        record.get("history") or []
    ) + [history_entry]

    return attendance_response(new)


# ---------------------------------------------------------------------------
# Monthly employee analytics
# ---------------------------------------------------------------------------

def employee_monthly_pipeline(emp_code: str, month: str):
    bounds = month_match(month)

    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", bounds["$gte"]]},
                                    {"$lte": ["$date", bounds["$lte"]]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", PRESENCE]},
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "leave_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },
                            "total_overtime_minutes": {
                                "$sum": "$overtime_minutes"
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$unwind": {
                "path": "$stats",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "joined_on": 1,
                "present_days": {
                    "$ifNull": ["$stats.present_days", 0]
                },
                "leave_days": {
                    "$ifNull": ["$stats.leave_days", 0]
                },
                "late_count": {
                    "$ifNull": ["$stats.late_count", 0]
                },
                "total_late_minutes": {
                    "$ifNull": ["$stats.total_late_minutes", 0]
                },
                "total_overtime_minutes": {
                    "$ifNull": ["$stats.total_overtime_minutes", 0]
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(...),
):
    first, last = validate_month(month)

    pipeline = employee_monthly_pipeline(emp_code, month)
    rows = list(db.employees.aggregate(pipeline))

    if not rows:
        raise HTTPException(404, "Employee not found")

    row = rows[0]

    join_date = validate_date(row["joined_on"], "joined_on")
    working_start = max(first, join_date)
    days = working_days(working_start, last)

    present = round_half_up(row["present_days"], 2)
    pct = round_half_up(present * 100 / days, 2) if days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": row["leave_days"],
        "late_count": row["late_count"],
        "total_late_minutes": row["total_late_minutes"],
        "total_overtime_minutes": row["total_overtime_minutes"],
        "attendance_pct": pct,
    }


# ---------------------------------------------------------------------------
# Department monthly summary
# ---------------------------------------------------------------------------

def department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
):
    bounds = month_match(month)

    match = {"joined_on": {"$lte": bounds["$lte"]}}

    if department:
        match["department"] = department

    return [
        {"$match": match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", bounds["$gte"]]},
                                    {"$lte": ["$date", bounds["$lte"]]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", PRESENCE]},
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", PRESENCE]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", PRESENCE]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$unwind": {
                "path": "$stats",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$ifNull": ["$stats.present_days", 0]
                },
                "avg_work_hours": {
                    "$cond": [
                        {
                            "$gt": [
                                {"$ifNull": ["$stats.hours_count", 0]},
                                0,
                            ]
                        },
                        {
                            "$divide": [
                                "$stats.hours_sum",
                                "$stats.hours_count",
                            ]
                        },
                        None,
                    ]
                },
                "late_count": {
                    "$ifNull": ["$stats.late_count", 0]
                },
                "total_late_minutes": {
                    "$ifNull": ["$stats.total_late_minutes", 0]
                },
                "leave_count": {
                    "$ifNull": ["$stats.leave_count", 0]
                },
                "on_duty_count": {
                    "$ifNull": ["$stats.on_duty_count", 0]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = None,
):
    validate_month(month)

    rows = list(
        db.employees.aggregate(
            department_summary_pipeline(month, department),
            allowDiskUse=True,
        )
    )

    items = []

    for row in rows:
        row["present_days"] = round_half_up(row["present_days"], 2)

        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = round_half_up(
                row["avg_work_hours"],
                2,
            )

        items.append(row)

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Late leaderboard with competition ranking
# ---------------------------------------------------------------------------

def leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str] = None,
):
    bounds = month_match(month)

    pipeline = [
        {
            "$match": {
                "date": bounds,
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ]

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)

    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(month, limit, department),
            allowDiskUse=True,
        )
    )

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Department daily trend with $densify and moving average
# ---------------------------------------------------------------------------

def department_trend_pipeline(
    department: str,
    start: date,
    end: date,
):
    start_dt = datetime.combine(start, datetime.min.time())
    end_exclusive = datetime.combine(
        end + timedelta(days=1),
        datetime.min.time(),
    )
    start_text = start.isoformat()

    daily_log_lookup = [
        {
            "$match": {
                "$expr": {"$eq": ["$date", "$$day"]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": None,
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENCE]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "day": {
                    "$dateFromString": {
                        "dateString": start_text
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$day",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_text": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_text"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "head",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": daily_log_lookup,
                "as": "logs",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$head.headcount", 0]},
                        0,
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$logs.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$logs.late_count", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = validate_date(from_date, "from")
    end = validate_date(to_date, "to")

    if end < start:
        fail422("'to' must be on or after 'from'")

    if (end - start).days + 1 > 92:
        fail422("Trend range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    rows = list(
        db.employees.aggregate(
            department_trend_pipeline(department, start, end),
            allowDiskUse=True,
        )
    )

    output = []

    for row in rows:
        for key in ("attendance_rate", "moving_avg_7d"):
            if row[key] is not None:
                row[key] = round_half_up(row[key], 4)

        row["present_count"] = round_half_up(
            row["present_count"],
            2,
        )

        output.append(row)

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": output,
    }


# ---------------------------------------------------------------------------
# Query explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        fail422("Unknown endpoint")

    if endpoint == "attendance_list":
        require_date_range(date_from, date_to)

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if status:
            if status not in STATUSES:
                fail422("Invalid status")
            query["status"] = status

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {
                    "date": -1,
                    "emp_code": 1,
                },
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }

        try:
            result = db.command(command)
            result = make_json_safe(result)

            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": result,
            }

        except PyMongoError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"MongoDB explain failed: {str(exc)}",
            )

    if not month and endpoint != "department_trend":
        fail422("month is required")

    if endpoint == "employee_monthly":
        if not emp_code:
            fail422("emp_code is required")

        employee_or_404(emp_code)
        pipeline = employee_monthly_pipeline(emp_code, month)
        collection = "employees"

    elif endpoint == "department_summary":
        pipeline = department_summary_pipeline(month, department)
        collection = "employees"

    elif endpoint == "late_leaderboard":
        pipeline = leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"

    else:
        if not department or not from_date or not to_date:
            fail422("department, from and to are required")

        start = validate_date(from_date, "from")
        end = validate_date(to_date, "to")

        if end < start or (end - start).days + 1 > 92:
            fail422("Invalid trend date range")

        pipeline = department_trend_pipeline(
            department,
            start,
            end,
        )
        collection = "employees"

    if month:
        validate_month(month)

    result = safe_pipeline_explain(collection, pipeline)

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": result,
    }
