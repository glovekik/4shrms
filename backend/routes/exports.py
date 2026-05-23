"""XLSX exports for HR.

Each endpoint mirrors a JSON report in routes/reports.py but streams an
.xlsx file. Frontend can offer both "view" (JSON) and "download"
(this) without duplicating query logic.
"""

import io
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse

from datetime import datetime
from typing import Optional

from openpyxl import Workbook

from database import db
from utils.dependencies import require_hr, require_hr_or_ceo

router = APIRouter()


def _xlsx_response(
    wb: Workbook, filename: str,
) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _write_header(ws, columns: list[str]) -> None:
    ws.append(columns)
    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)


# ================= USERS =================
@router.get("/users.xlsx")
async def export_users(
    _hr: dict = Depends(require_hr_or_ceo),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    _write_header(ws, [
        "id", "name", "email", "role", "tag", "employeeCode",
        "departmentId", "reportingManagerId", "status", "joiningDate",
    ])
    async for u in db.users.find().sort("name", 1):
        ws.append([
            str(u["_id"]),
            u.get("name", ""),
            u.get("email", ""),
            u.get("role", "USER"),
            u.get("tag", ""),
            u.get("employeeCode", ""),
            u.get("departmentId", ""),
            u.get("reportingManagerId", ""),
            u.get("status", ""),
            u.get("joiningDate", ""),
        ])
    return _xlsx_response(wb, "users.xlsx")


# ================= ATTENDANCE =================
@router.get("/attendance.xlsx")
async def export_attendance(
    fromDate: Optional[str] = Query(None),
    toDate: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    _write_header(ws, [
        "userId", "date", "attendanceType", "status", "isLate",
        "checkIn", "checkOut", "hoursWorked", "overtimeHours",
        "workNotes",
    ])

    query: dict = {}
    if fromDate:
        query.setdefault("date", {})["$gte"] = fromDate
    if toDate:
        query.setdefault("date", {})["$lte"] = toDate

    async for r in db.attendance.find(query).sort("date", -1):
        ws.append([
            r.get("userId", ""),
            r.get("date", ""),
            r.get("attendanceType", ""),
            r.get("status", ""),
            r.get("isLate", False),
            r["checkIn"].isoformat() if r.get("checkIn") else "",
            r["checkOut"].isoformat() if r.get("checkOut") else "",
            float(r.get("hoursWorked", 0) or 0),
            float(r.get("overtimeHours", 0) or 0),
            r.get("workNotes", ""),
        ])
    return _xlsx_response(wb, "attendance.xlsx")


# ================= LEAVE REQUESTS =================
@router.get("/leave-requests.xlsx")
async def export_leave_requests(
    status: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Leave"
    _write_header(ws, [
        "id", "userId", "leaveTypeCode", "fromDate", "toDate",
        "totalDays", "halfDay", "status", "decidedBy", "decisionNote",
    ])
    query: dict = {}
    if status:
        query["status"] = status
    async for r in db.leave_requests.find(query).sort(
        "createdAt", -1
    ):
        ws.append([
            str(r["_id"]),
            r.get("userId", ""),
            r.get("leaveTypeCode", ""),
            r.get("fromDate", ""),
            r.get("toDate", ""),
            float(r.get("totalDays", 0) or 0),
            r.get("halfDay", False),
            r.get("status", ""),
            r.get("decidedBy", ""),
            r.get("decisionNote", ""),
        ])
    return _xlsx_response(wb, "leave-requests.xlsx")


# ================= OFFICE EXPENSES =================
@router.get("/expenses.xlsx")
async def export_expenses(
    fromDate: Optional[str] = Query(None, alias="from"),
    toDate: Optional[str] = Query(None, alias="to"),
    category: Optional[str] = Query(None),
    _hr: dict = Depends(require_hr_or_ceo),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Expenses"
    _write_header(ws, [
        "id", "date", "title", "category", "amount",
        "vendor", "paymentMethod", "description",
        "receiptUrl", "createdAt",
    ])

    query: dict = {}
    if fromDate:
        query.setdefault("date", {})["$gte"] = fromDate
    if toDate:
        query.setdefault("date", {})["$lte"] = toDate
    if category:
        query["category"] = category

    async for e in db.expenses.find(query).sort("date", -1):
        ws.append([
            str(e["_id"]),
            e.get("date", ""),
            e.get("title", ""),
            e.get("category", ""),
            float(e.get("amount", 0) or 0),
            e.get("vendor", ""),
            e.get("paymentMethod", ""),
            e.get("description", ""),
            e.get("receiptUrl", ""),
            e["createdAt"].isoformat() if e.get("createdAt") else "",
        ])

    name = "expenses"
    if fromDate or toDate:
        name = f"expenses_{fromDate or 'start'}_{toDate or 'end'}"
    return _xlsx_response(wb, f"{name}.xlsx")


# ================= PAYROLL =================
@router.get("/payroll/{year}/{month}.xlsx")
async def export_payroll(
    year: int,
    month: int,
    _hr: dict = Depends(require_hr_or_ceo),
):
    if month < 1 or month > 12:
        raise HTTPException(400, "month must be 1..12")

    wb = Workbook()
    ws = wb.active
    ws.title = f"{year}-{month:02d}"
    _write_header(ws, [
        "userId", "name", "totalGross", "totalDeductions", "netPay", "status",
    ])
    async for p in db.payslips.find({"year": year, "month": month}):
        ws.append([
            p.get("userId", ""),
            p.get("employeeName") or p.get("name") or "",
            float(p.get("totalGross", 0) or 0),
            float(p.get("totalDeductions", 0) or 0),
            float(p.get("netPay", 0) or 0),
            p.get("status", ""),
        ])
    return _xlsx_response(wb, f"payroll-{year}-{month:02d}.xlsx")
