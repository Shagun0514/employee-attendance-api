
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}
ALL_STATUSES = PRESENT_STATUSES | NON_PRESENT_STATUSES
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    """Create indexes idempotently before serving requests."""
    client.admin.command("ping")

    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_emp",
    )


# ---------------------------------------------------------------------------
# Shared validation and time helpers
# ---------------------------------------------------------------------------

def fail_422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail_422("Date must be in YYYY-MM-DD format")
    if parsed.isoformat() != value:
        fail_422("Date must be in YYYY-MM-DD format")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except (TypeError, ValueError):
        fail_422("Month must be in YYYY-MM format")
    if parsed.strftime("%Y-%m") != value:
        fail_422("Month must be in YYYY-MM format")
    start = parsed.replace(day=1)
    if parsed.month == 12:
        end = date(parsed.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(parsed.year, parsed.month + 1, 1) - timedelta(days=1)
    return start, end


def parse_shift(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except (TypeError, ValueError):
        fail_422("Shift time must be HH:MM")
    if parsed.second or parsed.microsecond or len(value) != 5:
        fail_422("Shift time must be HH:MM")
    return parsed


def parse_epoch_ms(value: int) -> datetime:
    # bool is a subclass of int, so reject it explicitly.
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS
    ):
        fail_422("Timestamp must be an integer epoch-millisecond value")
    try:
        instant = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        fail_422("Timestamp is out of range")
    # All calculations and storage use whole seconds.
    return instant.replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def public_employee(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = to_epoch_ms(result["created_at"])
    return result


def public_attendance(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    for key in ("punch_in", "punch_out"):
        if isinstance(result.get(key), datetime):
            result[key] = to_epoch_ms(result[key])
    for entry in result.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for field in ("from", "to"):
                if isinstance(change.get(field), datetime):
                    change[field] = to_epoch_ms(change[field])
    return result


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(
        pattern=r"^EMP\d{4,6}$"
    )
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: date

    @field_validator("joined_on", mode="before")
    @classmethod
    def validate_joined_on_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("joined_on must be a YYYY-MM-DD date string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a YYYY-MM-DD date string")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be a YYYY-MM-DD date string")
        return value

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self



class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    status: str = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return values


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeIn):
    start = parse_shift(payload.shift_start)
    end = parse_shift(payload.shift_end)

    employee = {
        "emp_code": payload.emp_code,
        "name": payload.name,
        "email": payload.email,
        "department": payload.department,
        "shift_start": start.strftime("%H:%M"),
        "shift_end": end.strftime("%H:%M"),
        "joined_on": payload.joined_on.isoformat(),
        "created_at": datetime.now(UTC).replace(microsecond=0),
    }

    try:
        db.employees.insert_one(employee)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee code already exists",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

    return public_employee(employee)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    filters = {}
    if department is not None:
        filters["department"] = department

    total = db.employees.count_documents(filters)
    cursor = (
        db.employees.find(filters, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [public_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



# ---------------------------------------------------------------------------
# Attendance punch-in
# ---------------------------------------------------------------------------

def attendance_date_for_punch(local_dt: datetime, employee: dict) -> date:
    """Determine attendance date using IST and overnight shift rules."""
    local_day = local_dt.date()
    shift_start = parse_shift(employee["shift_start"])
    shift_end = parse_shift(employee["shift_end"])

    if shift_end <= shift_start and local_dt.time().replace(tzinfo=None) < shift_end:
        return local_day - timedelta(days=1)

    return local_day


def calculate_late_minutes(
    punch_in: datetime,
    attendance_day: date,
    employee: dict,
) -> int:
    """Apply the 10-minute grace period; count whole minutes from shift start."""
    shift_start = parse_shift(employee["shift_start"])

    shift_start_local = datetime.combine(
        attendance_day,
        shift_start,
        tzinfo=IST,
    )
    shift_start_utc = shift_start_local.astimezone(UTC)

    elapsed_seconds = int((punch_in - shift_start_utc).total_seconds())

    # More than 10 minutes late is required.
    if elapsed_seconds <= 600:
        return 0

    return elapsed_seconds // 60


@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInIn):
    if payload.status not in PRESENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    employee = db.employees.find_one({"emp_code": payload.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if payload.punched_at is None:
        punched_at = datetime.now(UTC).replace(microsecond=0)
    else:
        punched_at = parse_epoch_ms(payload.punched_at)

    local_dt = punched_at.astimezone(IST)
    attendance_day = attendance_date_for_punch(local_dt, employee)

    late_minutes = calculate_late_minutes(
        punched_at,
        attendance_day,
        employee,
    )

    record = {
        "emp_code": payload.emp_code,
        "date": attendance_day.isoformat(),
        "punch_in": punched_at,
        "punch_out": None,
        "status": payload.status,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

    return public_attendance(record)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_null_optional_values(cls, value):
        if value is None:
            raise ValueError("Field cannot be null when supplied")
        return value

    @model_validator(mode="after")
    def validate_regularization(self):
        if self.status is not None and self.status not in ALL_STATUSES:
            raise ValueError("Invalid attendance status")

        for field_name in ("punch_in", "punch_out"):
            value = getattr(self, field_name)
            if value is not None:
                # Reject booleans, floats, strings, and invalid epoch ranges.
                if type(value) is not int or not (
                    MIN_EPOCH_MS <= value <= MAX_EPOCH_MS
                ):
                    raise ValueError(
                        f"{field_name} must be a valid epoch-millisecond integer"
                    )

        return self



# ---------------------------------------------------------------------------
# Attendance punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(payload: PunchInIn):
    # Resolve the timestamp, truncating it to whole seconds.
    if payload.punched_at is None:
        punched_at = datetime.now(UTC).replace(microsecond=0)
    else:
        punched_at = parse_epoch_ms(payload.punched_at)

    # Select the most recent eligible record first, even if it is already closed.
    eligible = db.attendance_logs.find_one(
        {
            "emp_code": payload.emp_code,
            "punch_in": {"$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if eligible is None:
        raise HTTPException(
            status_code=404,
            detail="No eligible attendance record found",
        )
    if eligible.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already closed",
        )

    open_record = eligible

    punch_in_time = open_record["punch_in"]
    elapsed_seconds = int((punched_at - punch_in_time).total_seconds())

    if elapsed_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be within 24 hours of punch-in",
        )

    employee = db.employees.find_one(
        {"emp_code": payload.emp_code}
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    attendance_day = parse_date(open_record["date"])
    shift_start = parse_shift(employee["shift_start"])
    shift_end = parse_shift(employee["shift_end"])

    shift_end_day = attendance_day
    if shift_end <= shift_start:
        shift_end_day += timedelta(days=1)

    shift_end_local = datetime.combine(
        shift_end_day,
        shift_end,
        tzinfo=IST,
    )
    shift_end_utc = shift_end_local.astimezone(UTC)

    overtime_seconds = int((punched_at - shift_end_utc).total_seconds())
    overtime_minutes = (
        overtime_seconds // 60
        if overtime_seconds >= 30 * 60
        else 0
    )

    work_hours = round_half_up(elapsed_seconds / 3600, 2)
    half_day = work_hours < 4.50

    try:
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": open_record["_id"],
                "punch_in": open_record["punch_in"],
                "punch_out": None,
            },
            {
                "$set": {
                    "punch_out": punched_at,
                    "work_hours": work_hours,
                    "overtime_minutes": overtime_minutes,
                    "half_day": half_day,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was closed by another request",
        )

    return public_attendance(updated)


# ---------------------------------------------------------------------------
# Attendance listing with database-side filtering and pagination
# ---------------------------------------------------------------------------

def build_attendance_filters(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    filters = {}

    if emp_code is not None:
        filters["emp_code"] = emp_code

    if date_from is not None:
        filters["date"] = {
            "$gte": parse_date(date_from).isoformat()
        }

    if date_to is not None:
        if "date" not in filters:
            filters["date"] = {}
        filters["date"]["$lte"] = parse_date(date_to).isoformat()

    if date_from is not None and date_to is not None:
        if parse_date(date_from) > parse_date(date_to):
            raise HTTPException(
                status_code=422,
                detail="date_from must not be after date_to",
            )

    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status",
            )
        filters["status"] = status

    return filters

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):

    filters = build_attendance_filters(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )

    total = db.attendance_logs.count_documents(filters)

    cursor = (
        db.attendance_logs.find(filters, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [public_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




# ---------------------------------------------------------------------------
# Attendance regularization with append-only audit history
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    payload: RegularizeIn,
):
    attendance_day = parse_date(date)
    day_string = attendance_day.isoformat()

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    current = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": day_string}
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    updates = {}
    if payload.status is not None:
        updates["status"] = payload.status
    if "punch_in" in payload.model_fields_set:
        updates["punch_in"] = (
            parse_epoch_ms(payload.punch_in)
            if payload.punch_in is not None else None
        )
    if "punch_out" in payload.model_fields_set:
        updates["punch_out"] = (
            parse_epoch_ms(payload.punch_out)
            if payload.punch_out is not None else None
        )

    status = updates.get("status", current["status"])
    punch_in_time = updates.get("punch_in", current.get("punch_in"))
    punch_out_time = updates.get("punch_out", current.get("punch_out"))

    # Non-present statuses cannot have punch times.
    if status in NON_PRESENT_STATUSES:
        if "punch_in" in payload.model_fields_set:
            raise HTTPException(status_code=422, detail="Non-present status cannot have punch times")
        if "punch_out" in payload.model_fields_set:
            raise HTTPException(status_code=422, detail="Non-present status cannot have punch times")
        punch_in_time = None
        punch_out_time = None
        updates["punch_in"] = None
        updates["punch_out"] = None
    else:
        if punch_in_time is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        local_punch_in = punch_in_time.astimezone(IST)
        expected_day = attendance_date_for_punch(local_punch_in, employee)
        if expected_day != attendance_day:
            raise HTTPException(status_code=422, detail="punch_in does not match attendance date")

        if punch_out_time is not None:
            elapsed = (punch_out_time - punch_in_time).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute all derived values from the final status and timestamps.
    if status in NON_PRESENT_STATUSES:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = calculate_late_minutes(
            punch_in_time, attendance_day, employee
        )

        if punch_out_time is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            elapsed_seconds = int(
                (punch_out_time - punch_in_time).total_seconds()
            )
            work_hours = round_half_up(elapsed_seconds / 3600, 2)
            half_day = work_hours < 4.50

            shift_start = parse_shift(employee["shift_start"])
            shift_end = parse_shift(employee["shift_end"])
            shift_end_day = attendance_day
            if shift_end <= shift_start:
                shift_end_day += timedelta(days=1)

            shift_end_utc = datetime.combine(
                shift_end_day, shift_end, tzinfo=IST
            ).astimezone(UTC)
            overtime_seconds = int(
                (punch_out_time - shift_end_utc).total_seconds()
            )
            overtime_minutes = (
                overtime_seconds // 60
                if overtime_seconds >= 30 * 60 else 0
            )

    updates.update({
        "status": status,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    })

    # Keep only actual changes in the audit entry.
    changes = {}
    for field, new_value in updates.items():
        old_value = current.get(field)
        if old_value != new_value:
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": to_epoch_ms(old_value),
                    "to": to_epoch_ms(new_value),
                }
            else:
                changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="Regularization made no changes")

    audit_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }

    history_guard = (
        {"history": current["history"]}
        if "history" in current
        else {"history": {"$exists": False}}
    )

    # Conditional update prevents concurrent corrections from losing history.
    try:
        result = db.attendance_logs.update_one(
            {
                "_id": current["_id"],
                **history_guard,
                "status": current["status"],
                "punch_in": current.get("punch_in"),
                "punch_out": current.get("punch_out"),
            },
            {
                "$set": updates,
                "$push": {"history": audit_entry},
            },
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one({"_id": current["_id"]})
    return public_attendance(updated)



def count_weekdays(start_date: date, end_date: date) -> int:
    """Count Monday-Friday dates, including both endpoints."""
    if start_date > end_date:
        return 0

    total = 0
    current = start_date

    while current <= end_date:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)

    return total


def build_employee_monthly_pipeline(
    emp_code: str,
    month_start: date,
    month_end: date,
) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": "$date_obj"},
                        [2, 3, 4, 5, 6],
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
                                    "$is_weekday",
                                    {
                                        "$in": [
                                            "$status",
                                            list(PRESENT_STATUSES),
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
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
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    # Parse the requested month, such as 2026-10.
    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    # An unknown employee should return 404.
    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    joined_on = date.fromisoformat(employee["joined_on"])
    working_start = max(joined_on, month_start)

    working_days = count_weekdays(working_start, month_end)


    # Build the monthly aggregation using the shared helper.
    pipeline = build_employee_monthly_pipeline(
        emp_code=emp_code,
        month_start=month_start,
        month_end=month_end,
    )


    result = next(db.attendance_logs.aggregate(pipeline), {})

    present_days = round_half_up(result.get("present_days", 0), 2)

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 4)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month_start: date,
    month_end: date,
    department: Optional[str] = None,
) -> list:
    employee_match = {}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": "$date_obj"},
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$addFields": {
                "joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "joined_during_month": {
                    "$lte": [
                        "$joined_date",
                        {
                            "$dateFromString": {
                                "dateString": month_end.isoformat(),
                                "format": "%Y-%m-%d",
                            }
                        },
                    ]
                }
            }
        },
        {"$match": {"joined_during_month": True}},
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$attendance.is_weekday", True]},
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            list(PRESENT_STATUSES),
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$attendance.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            list(PRESENT_STATUSES),
                                        ]
                                    },
                                    {"$ne": ["$attendance.work_hours", None]},
                                ]
                            },
                            "$attendance.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$attendance.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$attendance.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$headcount"},
                "valid_work_hours": {
                    "$filter": {
                        "input": "$work_hours_values",
                        "as": "hours",
                        "cond": {"$ne": ["$$hours", None]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None,
                    ]
                }
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = Query(None),
):
    # Parse the requested month.
    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    # Build one pipeline that starts from employees so that employees
    # with no attendance records still contribute to headcount.
    employee_match = {}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": "$date_obj"},
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$addFields": {
                "joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "joined_during_month": {
                    "$lte": ["$joined_date", {
                        "$dateFromString": {
                            "dateString": month_end.isoformat(),
                            "format": "%Y-%m-%d",
                        }
                    }]
                }
            }
        },
        {
            "$match": {
                "joined_during_month": True
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$attendance.is_weekday", True]},
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            list(PRESENT_STATUSES),
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$attendance.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            list(PRESENT_STATUSES),
                                        ]
                                    },
                                    {"$ne": ["$attendance.work_hours", None]},
                                ]
                            },
                            "$attendance.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$attendance.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$attendance.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$headcount"},
                "valid_work_hours": {
                    "$filter": {
                        "input": "$work_hours_values",
                        "as": "hours",
                        "cond": {"$ne": ["$$hours", None]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None,
                    ]
                }
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))

    # Apply the required HALF_UP rounding to the average.
    for item in results:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(
                item["avg_work_hours"], 2
            )

    return {
        "month": month,
        "items": results,
    }


def build_late_leaderboard_pipeline(
    month_start: date,
    month_end: date,
    limit: int,
    department: Optional[str] = None,
) -> list:
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
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

    # Filter before ranking so ranks are calculated within the department.
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        # Apply the limit after ranking, preserving ties at the cutoff.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    try:
        month_start, month_end = parse_month(month)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    pipeline = build_late_leaderboard_pipeline(
        month_start=month_start,
        month_end=month_end,
        limit=limit,
        department=department,
    )

    results = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": results,
    }


def build_department_trend_pipeline(
    department: str,
    start_day: date,
    end_day: date,
) -> list:
    start_datetime = datetime.combine(
        start_day, time.min, tzinfo=timezone.utc
    )
    end_datetime = datetime.combine(
        end_day, time.min, tzinfo=timezone.utc
    )

    pipeline = [
        # Generate every calendar date inside MongoDB.
        {
            "$documents": [
                {
                    "start": start_datetime,
                    "end": end_datetime,
                }
            ]
        },
        {
            "$project": {
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$start",
                                                "endDate": "$end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$start",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$dates"},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$dates",
                        "timezone": "UTC",
                    }
                },
                "day_of_week": {"$dayOfWeek": "$dates"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": ["$day_of_week", [2, 3, 4, 5, 6]]
                }
            }
        },
        # Count employees who have joined by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$department",
                                            department,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$current_date",
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "employee_count",
            }
        },
        # Aggregate attendance for this department on each date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$current_date"]
                            }
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
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                list(PRESENT_STATUSES),
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                "$half_day",
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
                                        {
                                            "$gt": [
                                                {
                                                    "$ifNull": [
                                                        "$late_minutes",
                                                        0,
                                                    ]
                                                },
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_attendance",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$employee_count.headcount",
                                0,
                            ]
                        },
                        0,
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_attendance.present_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_attendance.late_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                }
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
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
    ]

    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate the requested date range.
    try:
        start_day = parse_date(from_date)
        end_day = parse_date(to_date)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="from and to must be valid YYYY-MM-DD dates",
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from",
        )

    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Return 404 if the department has no employees.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    pipeline = build_department_trend_pipeline(
        department=department,
        start_day=start_day,
        end_day=end_day,
    )

    # Database-level aggregation starts with $documents.
    results = list(db.aggregate(pipeline))

    # Use HALF_UP rounding for reported rates.
    for item in results:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item[field] is not None:
                item[field] = round_half_up(item[field], 4)

        item["present_count"] = round_half_up(
            item["present_count"], 2
        )

    return {
        "department": department,
        "items": results,
    }


{"$addFields": {
    "attendance_rate": {
        "$cond": [
            {
                "$and": [
                    "$is_working_day",
                    {"$gt": ["$headcount", 0]}
                ]
            },
            {"$divide": ["$present_count", "$headcount"]},
            None
        ]
    }
}},





@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint not in {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }:
        raise HTTPException(
            status_code=422,
            detail="Invalid endpoint name",
        )


    if endpoint == "attendance_list":
        filters = build_attendance_filters(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )

        explain_result = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": filters,
                "projection": {"_id": 0},
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }



    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=422,
                detail="emp_code is required for employee_monthly",
            )

        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for employee_monthly",
            )

        try:
            month_start, month_end = parse_month(month)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="month must be in YYYY-MM format",
            )

        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        pipeline = build_employee_monthly_pipeline(
            emp_code=emp_code,
            month_start=month_start,
            month_end=month_end,
        )

        explain_result = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }



    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )

        try:
            month_start, month_end = parse_month(month)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="month must be in YYYY-MM format",
            )

        pipeline = build_department_summary_pipeline(
            month_start=month_start,
            month_end=month_end,
            department=department,
        )

        explain_result = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_result,
        }


    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )

        try:
            month_start, month_end = parse_month(month)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="month must be in YYYY-MM format",
            )

        pipeline = build_late_leaderboard_pipeline(
            month_start=month_start,
            month_end=month_end,
            limit=limit,
            department=department,
        )

        explain_result = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == "department_trend":
        if department is None:
            raise HTTPException(
                status_code=422,
                detail="department is required for department_trend",
            )
        if from_date is None:
            raise HTTPException(
                status_code=422,
                detail="from is required for department_trend",
            )
        if to_date is None:
            raise HTTPException(
                status_code=422,
                detail="to is required for department_trend",
            )

        try:
            start_day = parse_date(from_date)
            end_day = parse_date(to_date)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="from and to must be valid YYYY-MM-DD dates",
            )

        if end_day < start_day:
            raise HTTPException(
                status_code=422,
                detail="to must be on or after from",
            )

        if (end_day - start_day).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        pipeline = build_department_trend_pipeline(
            department=department,
            start_day=start_day,
            end_day=end_day,
        )

        explain_result = db.command({
            "explain": {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "database",
            "explain": explain_result,
        }
