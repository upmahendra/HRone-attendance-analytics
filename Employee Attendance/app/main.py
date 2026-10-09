"""Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
"""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("late_minutes", 1)])


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


def is_valid_epoch_ms(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 100000000000 <= value <= 4102444800000


def dt_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def value_to_api(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_epoch_ms(value)
    if isinstance(value, dict):
        return {k: value_to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [value_to_api(v) for v in value]
    return value


def quantize_half_up(value: Decimal, digits: int) -> Decimal:
    exp = Decimal("1").scaleb(-digits)
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def round_float(value: float | Decimal | None, digits: int) -> float | None:
    if value is None:
        return None
    q = Decimal(str(value))
    return float(quantize_half_up(q, digits))


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = map(int, value.split(":"))
    return h, m


def to_ist_datetime(milliseconds: int) -> datetime:
    dt = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).astimezone(IST)
    return dt.replace(microsecond=0)


def attendance_date_for_punched_at(emp: dict, punched_at: datetime) -> str:
    punched_at = punched_at.astimezone(IST).replace(microsecond=0)
    start_h, start_m = parse_hhmm(emp["shift_start"])
    end_h, end_m = parse_hhmm(emp["shift_end"])
    start_dt = datetime.combine(punched_at.date(), time(start_h, start_m), tzinfo=IST)
    end_dt = datetime.combine(punched_at.date(), time(end_h, end_m), tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        if punched_at.time() < time(end_h, end_m):
            start_dt = start_dt - timedelta(days=1)
            return start_dt.date().isoformat()
        return punched_at.date().isoformat()
    return punched_at.date().isoformat()


def shift_window_for_date(emp: dict, date_str: str) -> tuple[datetime, datetime]:
    d = date.fromisoformat(date_str)
    start_h, start_m = parse_hhmm(emp["shift_start"])
    end_h, end_m = parse_hhmm(emp["shift_end"])
    shift_start = datetime.combine(d, time(start_h, start_m), tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        shift_end = datetime.combine(d + timedelta(days=1), time(end_h, end_m), tzinfo=IST)
    else:
        shift_end = datetime.combine(d, time(end_h, end_m), tzinfo=IST)
    return shift_start, shift_end


def compute_late_minutes(punch_in: datetime, shift_start: str, emp: Optional[dict] = None) -> int:
    h, m = parse_hhmm(shift_start)
    punch_in_ist = punch_in.astimezone(IST).replace(microsecond=0)
    if emp is not None and emp["shift_end"] <= emp["shift_start"] and punch_in_ist.time() < time(*parse_hhmm(emp["shift_end"])):
        start_dt = datetime.combine(punch_in_ist.date(), time(h, m), tzinfo=IST) - timedelta(days=1)
    else:
        start_dt = datetime.combine(punch_in_ist.date(), time(h, m), tzinfo=IST)
    elapsed_seconds = (punch_in_ist - start_dt).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    delta = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
    return float(quantize_half_up(delta, 2))


def compute_overtime(punch_out: datetime, shift_end: str, record_date: str, emp: Optional[dict] = None) -> int:
    h, m = parse_hhmm(shift_end)
    if emp is not None and emp["shift_end"] <= emp["shift_start"]:
        _, shift_end_dt = shift_window_for_date(emp, record_date)
    else:
        shift_end_dt = datetime.combine(date.fromisoformat(record_date), time(h, m), tzinfo=IST)
    diff_min = int((punch_out.astimezone(IST) - shift_end_dt).total_seconds() / 60)
    return diff_min if diff_min >= 30 else 0


def last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def month_bounds(month: str) -> tuple[str, str]:
    year, month_num = map(int, month.split("-"))
    first = date(year, month_num, 1)
    last = date(year, month_num, last_day_of_month(year, month_num))
    return first.isoformat(), last.isoformat()


def is_weekday(date_str: str) -> bool:
    return date.fromisoformat(date_str).weekday() < 5


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def serialize_employee(doc: dict) -> dict:
    out = {k: value_to_api(v) for k, v in doc.items() if k != "_id"}
    return out


def serialize_attendance(doc: dict) -> dict:
    out = {k: value_to_api(v) for k, v in doc.items() if k != "_id"}
    return out


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - depends on Mongo availability
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    try:
        employee_doc = body.model_dump()
        employee_doc["created_at"] = datetime.now(timezone.utc)
        db.employees.insert_one(employee_doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(employee_doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    if body.status not in PRESENT_STATUSES:
        raise HTTPException(status_code=422, detail="status must be PRESENT, WFH or ON_DUTY")
    ts_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if not is_valid_epoch_ms(ts_ms):
        raise HTTPException(status_code=422, detail="punched_at is invalid")
    ts = to_ist_datetime(ts_ms)
    date_str = attendance_date_for_punched_at(emp, ts)
    try:
        doc = {
            "emp_code": body.emp_code,
            "date": date_str,
            "status": body.status,
            "punch_in": ts.astimezone(timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(ts.astimezone(IST), emp["shift_start"], emp),
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if not is_valid_epoch_ms(ts_ms):
        raise HTTPException(status_code=422, detail="punched_at is invalid")
    punched_at = to_ist_datetime(ts_ms)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at.astimezone(timezone.utc)}, "status": {"$in": list(PRESENT_STATUSES)}}, sort=[("punch_in", -1)])
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_dt = record["punch_in"].astimezone(IST)
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if punched_at - punch_in_dt > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt.astimezone(timezone.utc), punched_at.astimezone(timezone.utc))
    overtime_minutes = compute_overtime(punched_at, emp["shift_end"], record["date"], emp)
    half_day = work_hours < 4.50
    updated = {
        "punch_out": punched_at.astimezone(timezone.utc),
        "work_hours": float(Decimal(str(work_hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": updated})
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="record was updated concurrently")
    fresh = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(fresh)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    items = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    payload = body.model_dump(exclude_unset=True)
    if not payload or set(payload.keys()) == {"reason", "regularized_by"}:
        raise HTTPException(status_code=422, detail="request changes nothing")

    final_status = payload.get("status", current.get("status"))
    final_punch_in_ms = payload.get("punch_in")
    final_punch_out_ms = payload.get("punch_out")

    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in payload or "punch_out" in payload:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE records must not have punch times")
        final_punch_in_ms = None
        final_punch_out_ms = None
    else:
        if final_punch_in_ms is None:
            final_punch_in_ms = dt_to_epoch_ms(current.get("punch_in"))
        if final_punch_in_ms is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if "punch_out" in payload:
            final_punch_out_ms = payload.get("punch_out")
        else:
            final_punch_out_ms = dt_to_epoch_ms(current.get("punch_out"))

    if final_punch_in_ms is not None:
        final_punch_in_dt = to_ist_datetime(final_punch_in_ms)
        if attendance_date_for_punched_at(employee, final_punch_in_dt) != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the record date")
        if final_punch_out_ms is not None and final_punch_out_ms <= final_punch_in_ms:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if final_punch_out_ms is not None and final_punch_out_ms - final_punch_in_ms > 86_400_000:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours")

    if final_status in {"ABSENT", "LEAVE"}:
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
        final_punch_in_dt = None
        final_punch_out_dt = None
    else:
        final_punch_in_dt = to_ist_datetime(final_punch_in_ms)
        final_late_minutes = compute_late_minutes(final_punch_in_dt, employee["shift_start"], employee)
        if final_punch_out_ms is None:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False
            final_punch_out_dt = None
        else:
            final_punch_out_dt = to_ist_datetime(final_punch_out_ms)
            final_work_hours = compute_work_hours(final_punch_in_dt.astimezone(timezone.utc), final_punch_out_dt.astimezone(timezone.utc))
            final_overtime_minutes = compute_overtime(final_punch_out_dt, employee["shift_end"], date, employee)
            final_half_day = final_work_hours < 4.50

    old_values = {
        "status": current.get("status"),
        "punch_in": dt_to_epoch_ms(current.get("punch_in")),
        "punch_out": dt_to_epoch_ms(current.get("punch_out")),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    new_values = {
        "status": final_status,
        "punch_in": final_punch_in_ms,
        "punch_out": final_punch_out_ms,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    changes = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        if old_values[key] != new_values[key]:
            changes[key] = {"from": old_values[key], "to": new_values[key]}
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    new_history = current.get("history", []) + [{
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }]

    updated_doc = {
        "status": final_status,
        "punch_in": final_punch_in_dt.astimezone(timezone.utc) if final_punch_in_dt is not None else None,
        "punch_out": final_punch_out_dt.astimezone(timezone.utc) if final_punch_out_dt is not None else None,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
        "history": new_history,
    }
    result = db.attendance_logs.update_one(
        {"_id": current["_id"], "history": current.get("history", [])},
        {"$set": updated_doc},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="record was updated concurrently")
    updated = db.attendance_logs.find_one({"_id": current["_id"]})
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    month_start, month_end = month_bounds(month)
    docs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}))
    start_date = date.fromisoformat(month_start)
    end_date = date.fromisoformat(month_end)
    total_working_days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5 and employee["joined_on"] <= current.isoformat():
            total_working_days += 1
        current += timedelta(days=1)

    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for record in docs:
        if record.get("status") in {"ABSENT", "LEAVE"}:
            if record.get("status") == "LEAVE":
                leave_days += 1
            continue
        if is_weekday(record["date"]):
            if record.get("half_day"):
                present_days += Decimal("0.5")
            else:
                present_days += Decimal("1")
        if record.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(record.get("late_minutes", 0))
        total_overtime_minutes += int(record.get("overtime_minutes", 0))
    attendance_pct = None if total_working_days == 0 else float((present_days / Decimal(total_working_days)) * Decimal("100"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": total_working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": round_float(attendance_pct, 4) if attendance_pct is not None else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    month_start, month_end = month_bounds(month)
    employee_query = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_query["department"] = department
    employees = list(db.employees.find(employee_query, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    if not employees:
        return {"month": month, "items": []}
    department_map = {}
    for employee in employees:
        dept = employee["department"]
        department_map.setdefault(dept, {"headcount": 0, "present_days": Decimal("0"), "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "work_hours_sum": Decimal("0"), "work_hours_count": 0})
        department_map[dept]["headcount"] += 1

    records = list(db.attendance_logs.find({"date": {"$gte": month_start, "$lte": month_end}}))
    for record in records:
        emp = db.employees.find_one({"emp_code": record["emp_code"]}, {"_id": 0, "department": 1})
        if emp is None:
            continue
        dept = emp["department"]
        if dept not in department_map:
            continue
        if record.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and is_weekday(record["date"]):
            if record.get("half_day"):
                department_map[dept]["present_days"] += Decimal("0.5")
            else:
                department_map[dept]["present_days"] += Decimal("1")
        if record.get("late_minutes", 0) > 0:
            department_map[dept]["late_count"] += 1
            department_map[dept]["total_late_minutes"] += int(record.get("late_minutes", 0))
        if record.get("status") == "LEAVE":
            department_map[dept]["leave_count"] += 1
        if record.get("status") == "ON_DUTY":
            department_map[dept]["on_duty_count"] += 1
        if record.get("status") in PRESENT_STATUSES and record.get("work_hours") is not None:
            department_map[dept]["work_hours_sum"] += Decimal(str(record["work_hours"]))
            department_map[dept]["work_hours_count"] += 1

    items = []
    for dept in sorted(department_map):
        stats = department_map[dept]
        if stats["headcount"] == 0:
            continue
        avg_work_hours = None if stats["work_hours_count"] == 0 else float((stats["work_hours_sum"] / Decimal(stats["work_hours_count"])))
        items.append({
            "department": dept,
            "headcount": stats["headcount"],
            "present_days": float(stats["present_days"]),
            "avg_work_hours": round_float(avg_work_hours, 2) if avg_work_hours is not None else None,
            "late_count": stats["late_count"],
            "total_late_minutes": stats["total_late_minutes"],
            "leave_count": stats["leave_count"],
            "on_duty_count": stats["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(default=10, ge=1, le=50), department: Optional[str] = None):
    month_start, month_end = month_bounds(month)
    employee_query = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_query["department"] = department
    employees = list(db.employees.find(employee_query, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    employee_map = {emp["emp_code"]: emp for emp in employees}
    logs = list(db.attendance_logs.find({"date": {"$gte": month_start, "$lte": month_end}}))
    totals = {}
    for record in logs:
        emp = employee_map.get(record["emp_code"])
        if emp is None:
            continue
        totals.setdefault(record["emp_code"], {"total_late_minutes": 0, "late_count": 0})
        late_minutes = int(record.get("late_minutes", 0) or 0)
        if late_minutes > 0:
            totals[record["emp_code"]]["total_late_minutes"] += late_minutes
            totals[record["emp_code"]]["late_count"] += 1

    ranked = []
    for emp_code, values in totals.items():
        if values["total_late_minutes"] <= 0:
            continue
        emp = employee_map[emp_code]
        ranked.append({
            "emp_code": emp_code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": values["total_late_minutes"],
            "late_count": values["late_count"],
        })
    ranked.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))

    if not ranked:
        return {"month": month, "items": []}

    results = []
    last_total = None
    last_rank = 0
    for index, entry in enumerate(ranked, start=1):
        if last_total is None or entry["total_late_minutes"] != last_total:
            last_rank = index
            last_total = entry["total_late_minutes"]
        if last_rank <= limit:
            results.append({
                "rank": last_rank,
                "emp_code": entry["emp_code"],
                "name": entry["name"],
                "department": entry["department"],
                "total_late_minutes": entry["total_late_minutes"],
                "late_count": entry["late_count"],
            })
    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...), response_model=None):
    employee_query = {"department": department}
    employees = list(db.employees.find(employee_query, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="department not found")
    from_date = date.fromisoformat(from_)
    to_date = date.fromisoformat(to)
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    span_days = (to_date - from_date).days + 1
    if span_days > 92:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")

    all_dates = []
    current = from_date
    while current <= to_date:
        all_dates.append(current.isoformat())
        current += timedelta(days=1)

    department_emp_codes = {emp["emp_code"] for emp in employees}
    logs = list(db.attendance_logs.find({"emp_code": {"$in": list(department_emp_codes)}, "date": {"$gte": from_, "$lte": to}}))
    log_by_date = {}
    for record in logs:
        log_by_date.setdefault(record["date"], []).append(record)

    results = []
    headcount_by_day = {}
    moving_window = []
    for day in all_dates:
        day_date = date.fromisoformat(day)
        joined_on_or_before = sum(1 for emp in employees if emp["joined_on"] <= day)
        headcount_by_day[day] = joined_on_or_before
        present_count = Decimal("0")
        late_count = 0
        for record in log_by_date.get(day, []):
            if record.get("status") in PRESENT_STATUSES:
                present_count += Decimal("0.5") if record.get("half_day") else Decimal("1")
            if record.get("late_minutes", 0) > 0:
                late_count += 1
        rate = None
        if day_date.weekday() < 5 and headcount_by_day[day] > 0:
            rate = present_count / Decimal(headcount_by_day[day])
        row = {
            "date": day,
            "is_working_day": day_date.weekday() < 5,
            "headcount": headcount_by_day[day],
            "present_count": float(present_count),
            "late_count": late_count,
            "attendance_rate": round_float(rate, 4) if rate is not None else None,
            "moving_avg_7d": None,
        }
        if row["attendance_rate"] is not None:
            moving_window.append(row["attendance_rate"])
            if len(moving_window) > 7:
                moving_window.pop(0)
        if moving_window:
            row["moving_avg_7d"] = round_float(sum(moving_window) / len(moving_window), 4)
        results.append(row)
    return {"department": department, "items": results}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: Optional[int] = Query(default=None, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_: Optional[str] = Query(default=None, alias="from"), to: Optional[str] = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(status_code=422, detail="unknown endpoint")

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
        if status is not None:
            q["status"] = status
        explain = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        month_start, month_end = month_bounds(month)
        explain = db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}).sort("date", 1).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = month_bounds(month)
        q = {"date": {"$gte": month_start, "$lte": month_end}}
        if department is not None:
            employee_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            q["emp_code"] = {"$in": employee_codes}
        explain = db.attendance_logs.find(q).sort("date", 1).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = month_bounds(month)
        q = {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}
        if department is not None:
            employee_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            q["emp_code"] = {"$in": employee_codes}
        explain = db.attendance_logs.find(q).sort([("late_minutes", -1), ("emp_code", 1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if month is None or from_ is None or to is None:
        raise HTTPException(status_code=422, detail="month, from and to are required")
    dept_emp_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    q = {"emp_code": {"$in": dept_emp_codes}, "date": {"$gte": from_, "$lte": to}}
    explain = db.attendance_logs.find(q).sort("date", 1).explain("executionStats")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
