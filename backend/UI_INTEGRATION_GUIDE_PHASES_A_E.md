# UI Integration Guide — Phases A through E

This guide layers on top of `UI_INTEGRATION_GUIDE.md` and documents everything added or changed in Phases A–E. Read the baseline guide first; this one only covers new endpoints, new fields, and behavioral changes.

**Base URL (dev):** `http://localhost:8000`  ·  **Swagger UI:** `http://localhost:8000/docs`

**Auth, conventions, error format**: unchanged from the baseline guide.

---

## Table of Contents

1. [Quick Summary of What's New](#1-quick-summary)
2. [Breaking Changes & Migrations](#2-breaking-changes--migrations)
3. [Phase A — Foundation](#3-phase-a--foundation)
   - [3.1 New Role: MANAGER](#31-new-role-manager)
   - [3.2 Departments](#32-departments)
   - [3.3 Expanded employee profile](#33-expanded-employee-profile)
   - [3.4 Manager-scoped approval endpoints](#34-manager-scoped-approval-endpoints)
   - [3.5 Audit logs](#35-audit-logs)
4. [Phase B — UX Completion](#4-phase-b--ux-completion)
   - [4.1 Tasks — priorities, Ongoing status, attachments](#41-tasks)
   - [4.2 Attendance states + automation](#42-attendance-states)
   - [4.3 Leave — half-day + conflict 409](#43-leave-changes)
   - [4.4 In-app notifications feed](#44-notifications-feed)
   - [4.5 Dashboards (HR / Manager / Me)](#45-dashboards)
5. [Phase C — New Modules](#5-phase-c--new-modules)
   - [5.1 Projects](#51-projects)
   - [5.2 To-Do](#52-todos)
   - [5.3 Per-employee Documents](#53-documents)
   - [5.4 Expense Reimbursement flow](#54-reimbursements)
   - [5.5 Timesheets](#55-timesheets)
   - [5.6 Reports & Analytics](#56-reports)
   - [5.7 Excel exports](#57-excel-exports)
   - [5.8 OTP login (opt-in)](#58-otp-login)
6. [Phase D — Future Modules](#6-phase-d--future-modules)
   - [6.1 Recruitment / ATS](#61-recruitment--ats)
   - [6.2 Performance management](#62-performance-management)
7. [Phase E — Platform Features](#7-phase-e--platform-features)
   - [7.1 File uploads](#71-file-uploads)
   - [7.2 Public careers page](#72-public-careers)
   - [7.3 Public offer accept link](#73-public-offer-accept)
   - [7.4 SSE real-time notifications](#74-sse)
8. [Environment Variables](#8-environment-variables)
9. [Common Patterns for New Features](#9-common-patterns)
10. [End-to-End Smoke Test](#10-smoke-test)

---

## 1. Quick Summary

- **New role:** `MANAGER` (between USER and HR). HR can promote/demote.
- **Org structure:** `Department` + `reportingManagerId` + `projectManagerIds` on each user.
- **Approvals:** Manager or HR can decide on leave / corrections / reimbursements / timesheets / goals / reviews. Manager is scoped to their direct reports.
- **In-app notifications:** Real persistent feed with unread counts, plus optional SSE stream for instant push.
- **Dashboards:** One endpoint per audience (`/dashboard/hr`, `/dashboard/manager`, `/dashboard/me`).
- **8 new modules:** Projects, To-Do, Documents, Reimbursements, Timesheets, Reports, Excel exports, Recruitment, Performance.
- **File uploads:** `POST /uploads` returns a URL that any `*Url` field accepts.
- **Public endpoints (no auth):** `/careers/*` and `/public/offers/{token}/*` for the careers page + emailed offer-accept link.

---

## 2. Breaking Changes & Migrations

These are **not source-compatible** changes from the original guide.

### 2.1 Leave overlap is now `409 Conflict`

```
POST /leaves/request          (overlapping pending/approved range)
Before: 400 "An existing leave request overlaps these dates"
After:  409 "An existing leave request overlaps these dates"
```

Treat 409 as a user-facing conflict (show a friendly "you already have leave during these dates" message) rather than a generic validation error.

### 2.2 Attendance `status` has new values

The set used to be `CHECKED_IN | COMPLETED`. After Phase B, possible values are:

| Status | When |
|---|---|
| `CHECKED_IN` | Transient state between check-in and check-out (unchanged) |
| `PRESENT` | Checked out, ≥ 4.5 hours, on time |
| `LATE` | Checked out (or in), but check-in was after 10:15 + 15 min grace |
| `HALF_DAY` | Checked out with < 4.5 hours worked |
| `ABSENT` | Synthesized at 00:30 next day for working-day no-shows |
| `COMPLETED` | Legacy — still returned for old records |

A 4-state UI (CHECKED_IN / PRESENT / LATE / HALF_DAY) covers what the user will see day-to-day; ABSENT is for HR historical views.

Attendance responses now also include:
```json
{
  "isLate": false,
  "hoursWorked": 8.42,
  "overtimeHours": 0.0
}
```

### 2.3 `HRCreateUser` accepts `role` and many new optional fields

Existing clients sending only `{name, email, password, tag, employeeCode, ...}` keep working — every new field is optional. But you'll want to start sending:
- `role: "USER" | "MANAGER"` (default still `USER`)
- `departmentId`, `reportingManagerId`, `projectManagerIds[]`
- `work`, `personal`, `bankAccounts`, `emergencyContact`, `documents`, `statutory`, `contract` (all nested objects — see [3.3](#33-expanded-employee-profile))

### 2.4 `GET /hr/users/{id}` returns the expanded shape

The response now includes everything above. Existing fields are unchanged; just more fields present.

### 2.5 Task model has new fields and a new transition

- New field `priority`: `"LOW" | "MEDIUM" | "HIGH" | "CRITICAL"` (default `MEDIUM`)
- New field `attachments`: `string[]` (file URLs)
- New status `ONGOING` between `PENDING` and `COMPLETED`
- New field `startedAt`: ISO timestamp set when task transitions to ONGOING
- New endpoint `POST /tasks/{id}/start` (assignee only) to flip PENDING → ONGOING

Existing PENDING / COMPLETED flows continue to work without code changes.

### 2.6 `/auth/login` may return `{step: "OTP_REQUIRED"}`

Only when `REQUIRE_LOGIN_OTP=true` is set on the server (off by default). When on, login returns:
```json
{"step": "OTP_REQUIRED", "message": "An OTP has been sent to your email. Call /auth/verify-otp to complete login."}
```
…and the UI must POST to `/auth/verify-otp` with the emailed 6-digit code. See [5.8](#58-otp-login).

---

## 3. Phase A — Foundation

### 3.1 New Role: MANAGER

The `role` field on user docs can now be `"HR" | "MANAGER" | "USER"`.

| Role | What they can do |
|---|---|
| `HR` | Everything (unchanged). |
| `MANAGER` | Approve leave/correction/reimbursement/timesheet/goal/review for their **direct reports** (users where `reportingManagerId == manager.id`). Cannot create users. |
| `USER` | Standard employee. |

HR creates managers via `POST /hr/users` with `role: "MANAGER"`. HR can change role via `PUT /hr/users/{id}` with `role: "MANAGER" | "USER"` (cannot set `HR` — use the bootstrap script).

The existing `tag` field (Employee / Consultant / Intern / Manager / HR / Founder / CEO) is still purely informational — it doesn't grant permissions. Use `role` for permission decisions.

### 3.2 Departments

A department is a separate concept from a Team. Team = TL's working group for tasks; Department = HR's org structure for reporting/approvals.

```
GET    /departments                  (any authed user)
GET    /hr/departments/{id}          (HR only)
POST   /hr/departments               (HR only)
PUT    /hr/departments/{id}          (HR only)
DELETE /hr/departments/{id}          (HR only — refuses if any user still belongs)
```

Department shape:
```json
{
  "id": "65f...",
  "name": "Engineering",
  "description": "",
  "headUserId": "65a..."
}
```

Create body:
```json
{"name": "Engineering", "description": "", "headUserId": "65a..." }
```

`headUserId` is optional and is just a pointer (typically to a manager). It does not auto-confer permissions.

### 3.3 Expanded Employee Profile

`POST /hr/users` and `PUT /hr/users/{id}` accept (and `GET /hr/users/{id}` returns) these new optional fields. **All nested objects are optional, and every field within is optional** — pass only what you have.

```json
{
  "name": "...",
  "email": "...",
  "password": "...",
  "role": "USER",
  "tag": "Employee",
  "employeeCode": "E001",
  "workPhone": "+91...",
  "joiningDate": "2026-04-01",
  "status": "Active",
  "profilePictureUrl": "https://...",

  "departmentId": "65f...",
  "reportingManagerId": "65a...",
  "projectManagerIds": ["65b...", "65c..."],

  "work": {
    "departmentId": "65f...",          // can also be set in nested form
    "jobPosition": "Senior Engineer",
    "jobTitle": "Backend Lead",
    "reportingManagerId": "...",
    "projectManagerIds": ["..."],
    "workAddress": "...",
    "workLocation": "Bangalore Office",
    "usualWorkLocation": {
      "monday": "Office", "tuesday": "Office",
      "wednesday": "Home", "thursday": "Office",
      "friday": "Home", "saturday": null, "sunday": null
    },
    "notes": "..."
  },

  "personal": {
    "personalEmail": "...",
    "phone": "+91...",
    "legalName": "...",
    "birthday": "1995-06-15",
    "placeOfBirth": "...",
    "gender": "Female",
    "disabled": false,
    "bloodGroup": "O+",
    "maritalStatus": "Married",
    "address": {
      "street1": "...", "street2": "...", "city": "Bangalore",
      "state": "KA", "pinCode": "560001", "country": "IN"
    },
    "education": {
      "certificationLevel": "Master",   // Graduate | Bachelor | Master | Doctor | Other
      "fieldOfStudy": "Computer Science"
    }
  },

  "bankAccounts": [
    {
      "bankName": "HDFC",
      "accountNumber": "...",
      "ifscCode": "HDFC0001234",
      "branch": "Indiranagar",
      "accountHolderName": "..."
    }
  ],

  "emergencyContact": {
    "contactName": "...", "relationship": "Spouse", "phone": "+91..."
  },

  "documents": {
    // Single URL per category — for richer multi-file storage use /me/documents
    "idCardCopy": "https://...", "aadhaarCopy": "https://...",
    "panCopy": "...", "tenth": "...", "inter": "...", "ug": "...",
    "pg": "...", "phd": "...", "offerLetter": "...",
    "experienceLetter": "...", "resume": "...", "passport": "...",
    "relievingLetter": "...",
    "salarySlips": ["...", "..."],
    "certifications": ["...", "..."]
  },

  "statutory": {
    "pan": "ABCDE1234F", "uan": "...", "pfAccountNumber": "...", "esiNumber": "..."
  },

  "contract": {
    "contractStartDate": "2026-04-01",
    "contractEndDate": null,
    "wageType": "Fixed Wage",          // Fixed Wage | Hourly Wage
    "wage": 1500000,
    "wageDuration": "Year",            // Year | Half-Year | Quarter | 2 Months | Month | Half-Month | 2 Weeks | Week | Day
    "employeeType": "Employee"         // Employee | Worker | Student | Trainee | Contractor | Freelancer | Apprenticeship
  }
}
```

**To clear a scalar field on update**, send empty string `""` — backend converts it to null. To leave it unchanged, omit it.

**Nested objects on update replace the whole sub-doc** — to keep prior fields, send the whole object back with updates applied client-side.

`reportingManagerId` must reference a user whose role is `MANAGER` or `HR`. Validation will 400 otherwise.

### 3.4 Manager-scoped Approval Endpoints

All of these are accessible to `MANAGER` (scoped to their reports) **or** `HR` (sees everyone). Same body shapes as the existing HR variants.

```
GET    /manager/leave-requests?status=PENDING
POST   /manager/leave-requests/{id}/decide
       body: {"action": "APPROVE" | "REJECT", "note": "..."}

GET    /manager/correction-requests?status=PENDING
POST   /manager/correction-requests/{id}/decide
       body: {"action": "APPROVE" | "REJECT", "note": "...", "overrideCheckOut": "ISO 8601"}

GET    /manager/reimbursements?status=PENDING_MANAGER
POST   /manager/reimbursements/{id}/decide
       body: {"action": "APPROVE" | "REJECT", "note": "..."}

GET    /manager/timesheets?status=PENDING
POST   /manager/timesheets/{id}/decide
       body: {"action": "APPROVE" | "REJECT", "note": "..."}
```

UI guidance: when a manager logs in, show a single "Pending Approvals" widget that aggregates all four `/manager/...` lists. The dashboard endpoint ([4.5](#45-dashboards)) gives the counts in one call.

### 3.5 Audit Logs

```
GET /hr/audit-logs?actorId=&action=&entityType=&entityId=&fromDate=&toDate=&limit=100
```

HR-only. Returns rows like:
```json
{
  "id": "...",
  "actorId": "65a...",          // null for system/public actions
  "action": "leave.approve",
  "entityType": "leave_requests",
  "entityId": "65b...",
  "at": "2026-05-11T09:30:00+00:00",
  "before": { /* before state */ },
  "after":  { /* after state  */ },
  "metadata": null
}
```

Common `action` strings the UI may want to filter by:
- `user.create`, `user.update`, `role.change`
- `department.create`, `department.update`, `department.delete`
- `leave.approve`, `leave.reject`
- `correction.approve`, `correction.reject`
- `project.create`, `project.update`, `project.delete`
- `reimbursement.manager_approve`, `reimbursement.hr_approve`, `*.reject`
- `timesheet.approve`, `timesheet.reject`
- `candidate.create`, `candidate.move`, `candidate.apply_public`
- `interview.create`, `offer.create`, `offer.send`, `offer.accepted`, `offer.rejected`, `offer.revoke`, `offer.public_accepted`, `offer.public_rejected`
- `goal.create`, `review.create`, `review.submit`
- `document.upload`, `document.delete`

---

## 4. Phase B — UX Completion

### 4.1 Tasks

Field additions (TL routes `POST /tl/teams/{teamId}/tasks` and `PUT /tl/tasks/{id}`):
```json
{
  "title": "...",
  "description": "...",
  "assigneeId": "...",
  "priority": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "reminderIntervalMinutes": 30,
  "dueDate": "2026-05-20",
  "attachments": ["https://..."]
}
```

Status now has three values:
- `PENDING` — created, not started
- `ONGOING` — started by assignee
- `COMPLETED` — finished

New transition:
```
POST /tasks/{id}/start              (assignee only; PENDING → ONGOING)
```

Existing transitions:
```
POST /tasks/{id}/complete           (assignee; → COMPLETED, sets completedAt)
POST /tasks/{id}/uncomplete         (assignee; COMPLETED → ONGOING)
```

Response on every task now includes:
```json
{
  "priority": "MEDIUM",
  "attachments": [],
  "startedAt": "2026-05-11T09:00:00+00:00",
  "completedAt": null
}
```

### 4.2 Attendance States

`POST /attendance/checkin` response gained an `isLate` flag:
```json
{"message": "Check in successful", "isLate": true}
```

`POST /attendance/checkout` response now includes the final classification:
```json
{
  "message": "Checked out successfully",
  "status": "PRESENT" | "LATE" | "HALF_DAY",
  "hoursWorked": 8.42,
  "overtimeHours": 0.0
}
```

`GET /attendance/today` and `GET /attendance/history` now include `isLate`, `hoursWorked`, `overtimeHours`.

**Server-side rules** (configurable via env — see [8](#8-environment-variables)):
- `LATE` if check-in after 10:15 + 15 min grace
- `HALF_DAY` if hours worked < 4.5
- `overtimeHours = max(0, hoursWorked - 9)`
- `ABSENT` synthesized at 00:30 next day for active users with no check-in (skipped on weekends and `holidays` collection dates, and for users with an approved leave covering that date)

### 4.3 Leave Changes

- Overlap is now **409** (see [2.1](#21-leave-overlap-is-now-409-conflict)).
- `LeaveRequestCreate` already supported `halfDay`, `halfDayPart` (FIRST/SECOND), and `attachmentUrl` — no shape change. Backend computes `totalDays = 0.5` when `halfDay=true`.
- Manager endpoints in [3.4](#34-manager-scoped-approval-endpoints).

### 4.4 Notifications Feed

```
GET  /notifications?onlyUnread=false&limit=50&before=<ISO 8601>
GET  /notifications/unread-count
POST /notifications/{id}/read
POST /notifications/read-all
```

Notification shape:
```json
{
  "id": "...",
  "type": "leave_decision" | "correction_decision" | "task_assigned"
        | "task_complete"   | "reimbursement_decision"
        | "timesheet_decision" | "interview_scheduled"
        | "offer_response"  | "goal_assigned"
        | "review_started"  | "review_self_eval_submitted"
        | "review_submitted" | "feedback_received",
  "title": "Leave approved",
  "body": "2026-05-15 to 2026-05-17 (3 day(s))",
  "data": { "requestId": "65a...", "outcome": "APPROVED" },
  "read": false,
  "createdAt": "2026-05-11T09:30:00+00:00",
  "readAt": null
}
```

UI guidance:
- Show a bell icon with `GET /notifications/unread-count` badge (poll every ~30 s, or use SSE — see [7.4](#74-sse)).
- Tapping the bell opens `GET /notifications`. Use `data.*` to deep-link (e.g. `data.requestId` for leave approvals, `data.taskId` for tasks, etc.).
- POST `read` when the user opens a notification; POST `read-all` on a "mark all read" button.

Notifications are **also** emitted via push (FCM) and the in-app SSE channel — the UI doesn't have to choose. Use whichever pipe is convenient for that screen.

### 4.5 Dashboards

Three endpoints, one per audience, single JSON response each.

#### `GET /dashboard/hr`
```json
{
  "totalEmployees": 24,
  "presentToday": 18,
  "absentToday": 2,
  "onLeaveToday": 1,
  "pendingLeaveApprovals": 3,
  "pendingCorrectionApprovals": 1,
  "payrollStatus": {"year": 2026, "month": 4, "status": "FINALIZED"},
  "upcomingBirthdays": [
    {"id": "...", "name": "Asha", "birthday": "1996-05-18", "tag": "Engineer"}
  ],
  "employeeDistribution": [
    {"departmentId": "65f...", "departmentName": "Engineering", "count": 12},
    {"departmentId": null, "departmentName": "Unassigned", "count": 3}
  ]
}
```

#### `GET /dashboard/manager`
```json
{
  "directReports": 5,
  "teamAttendanceToday": [
    {"userId": "...", "status": "PRESENT", "isLate": false, "checkIn": "2026-05-11T09:01:..."}
  ],
  "pendingLeaveApprovals": 1,
  "pendingCorrectionApprovals": 0,
  "openTasksForReports": 4,
  "upcomingDeadlines": [
    {"id": "...", "title": "Ship login screen", "assigneeId": "...", "dueDate": "2026-05-14", "priority": "HIGH"}
  ]
}
```

#### `GET /dashboard/me`
```json
{
  "todayAttendance": {
    "status": "CHECKED_IN", "attendanceType": "OFFICE",
    "isLate": false, "checkIn": "...", "checkOut": null, "hoursWorked": 0
  },
  "leaveBalances": [
    {"code": "EARNED", "allocated": 12, "used": 3, "pending": 1, "remaining": 8}
  ],
  "openTasksCount": 4,
  "recentTasks": [
    {"id": "...", "title": "...", "status": "ONGOING", "priority": "MEDIUM", "dueDate": null}
  ],
  "pendingLeaveRequests": 1,
  "pendingCorrectionRequests": 0,
  "recentPayslips": [{"year": 2026, "month": 4, "netPay": 75000, "status": "FINALIZED"}],
  "unreadNotifications": 2
}
```

UI guidance: load the right dashboard endpoint on home screen mount; refresh on pull-to-refresh; don't poll. Use SSE for live notification badge updates instead.

---

## 5. Phase C — New Modules

### 5.1 Projects

```
GET    /projects                       (any authed)
GET    /projects/{id}
GET    /hr/projects/{id}               (HR only)
POST   /hr/projects                    (HR only)
PUT    /hr/projects/{id}               (HR only)
DELETE /hr/projects/{id}               (HR only)
```

Body:
```json
{
  "name": "Project Alpha",
  "code": "ALPHA",                 // unique, uppercased server-side
  "description": "...",
  "departmentId": "65f...",
  "projectManagerIds": ["65a..."],
  "memberIds": ["65b...", "65c..."],
  "status": "Active" | "OnHold" | "Completed",
  "startDate": "2026-04-01",
  "endDate": null,
  "billable": false
}
```

`code` collisions return 400. Used as the project allocation in [5.5 Timesheets](#55-timesheets).

### 5.2 Todos

Personal todos (separate from team tasks). Always scoped to the caller.

```
GET    /todos?status=OPEN&limit=100
POST   /todos
PUT    /todos/{id}
POST   /todos/{id}/complete
POST   /todos/{id}/reopen
DELETE /todos/{id}
```

Body:
```json
{
  "title": "Email vendor about renewal",
  "description": "...",
  "dueDate": "2026-05-15",
  "priority": "LOW" | "MEDIUM" | "HIGH",
  "reminderAt": "2026-05-15T09:00:00+00:00"  // ISO 8601 — UI schedules local notif
}
```

Response shape mirrors task but smaller. Status is `OPEN | DONE`.

### 5.3 Documents

Rich per-employee document store. Coexists with `user.documents` (legacy single-slot-per-category). Use this collection when you need:
- Multiple files per category (e.g. two ID copies)
- Upload metadata (who uploaded, when, notes, expiry date)

```
GET    /me/documents?category=PAN
POST   /me/documents
DELETE /me/documents/{id}

GET    /hr/users/{userId}/documents?category=Aadhaar
POST   /hr/users/{userId}/documents
DELETE /hr/users/{userId}/documents/{id}
```

Body:
```json
{
  "category": "PAN",
  "fileName": "pan-front.pdf",
  "fileUrl": "<url from POST /uploads>",
  "notes": "...",
  "expiresOn": "2030-12-31"
}
```

### 5.4 Reimbursements

Two-step approval flow: Employee → Manager → HR.

```
POST /expenses/reimbursements                 (employee — submit)
GET  /expenses/reimbursements/mine?status=

GET  /manager/reimbursements?status=PENDING_MANAGER
POST /manager/reimbursements/{id}/decide       (Manager or HR)

GET  /hr/reimbursements?status=PENDING_HR
POST /hr/reimbursements/{id}/decide            (HR final approval)
```

Submit body:
```json
{
  "title": "Client lunch — Wipro",
  "category": "Food",                  // free-form string; see PRD for full list
  "expenseDate": "2026-05-09",
  "amount": 2450.50,
  "paymentMode": "Credit Card",        // Cash | Bank Transfer | UPI | Credit Card | Debit Card | Company Wallet
  "vendorName": "Olive Beach",
  "invoiceNumber": "INV-7793",
  "taxAmount": 220,
  "description": "Lunch with client X",
  "attachments": ["<url from POST /uploads>"]
}
```

Status transitions:
```
PENDING_MANAGER → PENDING_HR (on manager APPROVE) | REJECTED (on manager REJECT)
PENDING_HR      → APPROVED                         | REJECTED (on HR REJECT)
```

Each transition emits a notification to the employee.

### 5.5 Timesheets

Weekly timesheets, **auto-built from attendance** unless the employee overrides.

#### Get my week
```
GET /timesheets/my?weekStart=2026-05-05    (weekStart MUST be a Monday)
```

If no timesheet exists for that week, returns a draft auto-built from `attendance.hoursWorked`:
```json
{
  "id": null,
  "userId": "...",
  "weekStart": "2026-05-05",
  "entries": [
    {"date": "2026-05-05", "hours": 8.42, "projectId": null, "notes": "", "billable": false, "attendanceStatus": "PRESENT"},
    {"date": "2026-05-06", "hours": 7.5,  "projectId": null, "notes": "", "billable": false, "attendanceStatus": "LATE"},
    ...
  ],
  "totalHours": 38.4,
  "status": "DRAFT",
  "draft": true
}
```

If submitted, returns the saved doc (`draft: false`).

#### Submit
```
POST /timesheets/submit
```
Body:
```json
{
  "weekStart": "2026-05-05",
  "note": "All hours on Project Alpha",
  "entries": [
    {"date": "2026-05-05", "hours": 8, "projectId": "65f...", "notes": "API work", "billable": true},
    ...
  ]
}
```
If `entries` omitted, backend uses the auto-built attendance hours.

Status: `PENDING → APPROVED | REJECTED`. Resubmit after a rejection works.

#### Manager + HR
```
GET  /manager/timesheets?status=PENDING
POST /manager/timesheets/{id}/decide       body: {"action": "APPROVE|REJECT", "note": ""}

GET  /hr/timesheets?status=&userId=&weekStart=&limit=100
```

### 5.6 Reports

HR-only JSON reports. All `fromDate`/`toDate` filters are `YYYY-MM-DD`.

| Endpoint | Returns |
|---|---|
| `GET /hr/reports/attendance?fromDate=&toDate=&departmentId=` | Per-user `{totalDays, present, late, halfDay, absent, leaveDays, totalHours, overtimeHours}` |
| `GET /hr/reports/leave?year=2026` | Per-user balance snapshot for the year |
| `GET /hr/reports/payroll?year=2026&month=4` | Payslip rows for the run |
| `GET /hr/reports/departments` | Headcount per department, including Unassigned |
| `GET /hr/reports/attrition?fromDate=&toDate=` | Approved exit rows in the window |
| `GET /manager/reports/team-productivity` | For each direct report: `{openTasks, completedTasksLast30d, avgHoursPerDayLast7d}` |

### 5.7 Excel Exports

HR-only XLSX downloads (same shapes as reports, but as a spreadsheet).

```
GET /hr/export/users.xlsx
GET /hr/export/attendance.xlsx?fromDate=&toDate=
GET /hr/export/leave-requests.xlsx?status=
GET /hr/export/payroll/{year}/{month}.xlsx
```

Frontend pattern: render an HTML `<a download href="...">` with the user's Bearer token in a header (or use `fetch()` + `blob()` + `URL.createObjectURL()`). The endpoints return `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.

### 5.8 OTP Login

**Off by default.** Only activates when the server has `REQUIRE_LOGIN_OTP=true`.

When enabled, the login flow becomes:
```
POST /auth/login                  (email, password)
  → 200 {"step": "OTP_REQUIRED", "message": "..."}
  → email sent with 6-digit code, valid 10 min

POST /auth/verify-otp             body: {"email": "...", "otp": "123456"}
  → 200 {"access_token": "...", "token_type": "bearer"}

POST /auth/resend-otp             body: {"email": "...", "otp": ""}
  → 200 {"message": "OTP sent"}
```

When `REQUIRE_LOGIN_OTP=false`, `/auth/login` returns the access token immediately (unchanged from baseline). UI should branch on whether the login response contains `step: "OTP_REQUIRED"`.

---

## 6. Phase D — Future Modules

### 6.1 Recruitment / ATS

All HR-only unless noted.

#### Job openings
```
GET    /hr/job-openings?status=Open
POST   /hr/job-openings
GET    /hr/job-openings/{id}
PUT    /hr/job-openings/{id}
DELETE /hr/job-openings/{id}
```
Body:
```json
{
  "title": "Senior Backend Engineer",
  "departmentId": "65f...",
  "location": "Bangalore",
  "employmentType": "Full-time" | "Part-time" | "Contract" | "Internship",
  "description": "...",
  "requirements": "...",
  "salaryMin": 1500000,
  "salaryMax": 2500000,
  "openings": 2,
  "status": "Open" | "OnHold" | "Closed"
}
```

#### Candidates
```
GET    /hr/candidates?stage=&jobOpeningId=&search=
POST   /hr/candidates
GET    /hr/candidates/{id}
PUT    /hr/candidates/{id}
DELETE /hr/candidates/{id}
POST   /hr/candidates/{id}/move            body: {"stage": "INTERVIEW", "note": "Cleared screening"}
```

Stages (pipeline order): `APPLIED → SCREENING → INTERVIEW → OFFER → HIRED | REJECTED | WITHDRAWN`. Each move appends to `stageHistory[]` on the candidate.

Candidate body:
```json
{
  "name": "...",
  "email": "...",
  "phone": "+91...",
  "jobOpeningId": "65f...",
  "resumeUrl": "<url from POST /uploads>",
  "source": "Referral" | "Job Portal" | "LinkedIn" | "Website" | "Walk-in" | "Agency" | "Other",
  "referredByUserId": "65a...",
  "currentCompany": "...",
  "currentSalary": 1200000,
  "expectedSalary": 1800000,
  "noticePeriodDays": 60,
  "notes": "..."
}
```

#### Interviews
```
GET  /hr/interviews?candidateId=&status=
POST /hr/interviews
PUT  /hr/interviews/{id}
POST /hr/interviews/{id}/feedback         (interviewer or HR)

GET  /interviews/mine                     (any user — interviews assigned to me)
```

Create body:
```json
{
  "candidateId": "65a...",
  "scheduledAt": "2026-05-12T15:00:00+00:00",
  "durationMinutes": 45,
  "mode": "In-person" | "Phone" | "Video",
  "location": "Meeting room 2 / Zoom link",
  "interviewerIds": ["65b...", "65c..."],
  "round": "Technical 1",
  "notes": "..."
}
```

Feedback body:
```json
{
  "rating": 4,                                          // 1..5
  "recommendation": "STRONG_HIRE" | "HIRE" | "NO_HIRE" | "STRONG_NO_HIRE",
  "strengths": "...",
  "concerns": "...",
  "notes": "..."
}
```

Submitting feedback flips interview status to `COMPLETED`. Resubmitting from the same interviewer replaces their prior entry (no stacking). Interviewers are notified when scheduled.

#### Offers
```
GET  /hr/offers?candidateId=&status=
POST /hr/offers
PUT  /hr/offers/{id}                       (only when status=DRAFT)
POST /hr/offers/{id}/send                  (DRAFT → SENT, emails candidate)
POST /hr/offers/{id}/record-decision       body: {"outcome": "ACCEPTED" | "REJECTED", "note": ""}
POST /hr/offers/{id}/revoke
```

Status flow: `DRAFT → SENT → ACCEPTED | REJECTED | EXPIRED | REVOKED`.

`POST /hr/offers/{id}/send`:
1. Generates a `publicToken`
2. Sends an email to the candidate including `OFFER_ACCEPT_URL_TEMPLATE.replace("{token}", ...)` if configured
3. Moves the candidate to stage `OFFER`

The candidate accepts/rejects via the **public** link (see [7.3](#73-public-offer-accept)), or HR records it manually via `record-decision`. Either way, on `ACCEPTED` the candidate moves to `HIRED`.

### 6.2 Performance Management

#### Goals (KPIs)
```
POST /manager/goals                       (manager/HR creates for direct report)
GET  /manager/goals?userId=&status=
PUT  /manager/goals/{id}

GET  /goals/mine?status=
POST /goals/{id}/progress                 body: {"achievedValue": 80, "note": "..."}

GET  /hr/goals?userId=&status=            (HR org-wide view)
```

Goal body:
```json
{
  "userId": "65a...",
  "title": "Lead the migration to v2 API",
  "description": "...",
  "dueDate": "2026-09-30",
  "targetValue": 100,                    // optional numeric KPI target
  "unit": "%",
  "weight": 0.3                          // optional 0..1 weight for review score
}
```

Status: `DRAFT | ACTIVE | COMPLETED | CANCELLED`.

#### Reviews

Four-step state machine:
```
SELF_EVAL → MANAGER_EVAL → SUBMITTED → ACKNOWLEDGED
```

```
POST /manager/reviews                                       (manager creates)
POST /reviews/{id}/self-eval                                (employee fills)
POST /manager/reviews/{id}/manager-eval                     (manager fills)
POST /manager/reviews/{id}/submit                           (manager submits)
POST /reviews/{id}/acknowledge                              (employee acknowledges)

GET  /reviews/mine
GET  /hr/reviews?employeeId=&status=
```

Create body:
```json
{
  "employeeId": "65a...",
  "type": "QUARTERLY" | "HALF_YEARLY" | "ANNUAL" | "PROMOTION" | "PROBATION",
  "periodStart": "2026-01-01",
  "periodEnd": "2026-03-31",
  "dimensions": ["Quality", "Ownership", "Collaboration"]   // optional; sensible defaults
}
```

Self-eval body:
```json
{
  "accomplishments": "...",
  "challenges": "...",
  "ratings": [{"dimension": "Quality", "rating": 4, "comment": "..."}],
  "overallSelfRating": 4
}
```

Manager-eval body:
```json
{
  "strengths": "...",
  "areasToImprove": "...",
  "ratings": [{"dimension": "Quality", "rating": 4, "comment": "..."}],
  "overallRating": 4,
  "promotionRecommendation": false,
  "nextSteps": "..."
}
```

After `submit`, the employee is notified to acknowledge. After `acknowledge`, the review is read-only.

#### 360° Feedback
```
POST /feedback                            body: {"toUserId": "...", "type": "POSITIVE", "text": "...", "anonymous": false}
GET  /feedback/about-me?type=
GET  /feedback/sent
GET  /hr/feedback?toUserId=&type=         (HR audit — sees senders even on anonymous)
```

Types: `POSITIVE | CONSTRUCTIVE | PEER | MANAGER_TO_REPORT | REPORT_TO_MANAGER`.

When `anonymous: true`, the recipient sees `fromUserId: null`, but HR and the sender themselves can still see who sent it.

---

## 7. Phase E — Platform Features

### 7.1 File Uploads

```
POST /uploads                    multipart/form-data with field "file"
```
Auth required. Response:
```json
{
  "url": "/static/uploads/2026/05/abc123-resume.pdf",   // or absolute URL if PUBLIC_BASE_URL is set
  "fileName": "resume.pdf",
  "size": 248311,
  "mimeType": "application/pdf",
  "uploadedBy": "65a..."
}
```

Use the returned `url` in any `*Url` field across the API (resume, attachment, profilePictureUrl, document fileUrl, etc.).

**Max size:** 20 MB by default (`MAX_UPLOAD_BYTES`).

**Serving:** Files are served back at `GET /static/uploads/<path>`. No auth on the read path — assume URLs are unguessable (they include a UUID prefix).

Frontend example (JS):
```js
const form = new FormData();
form.append("file", file);
const r = await fetch("/uploads", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
  body: form,
});
const { url } = await r.json();
```

### 7.2 Public Careers

**No auth required.** For a careers page hosted on the marketing site.

```
GET  /careers/openings                              (only status=Open returned)
GET  /careers/openings/{id}
POST /careers/openings/{id}/apply
```

Apply body:
```json
{
  "name": "...",
  "email": "...",
  "phone": "+91...",
  "resumeUrl": "<url — typically uploaded via POST /uploads from the careers UI>",
  "currentCompany": "...",
  "currentSalary": 1200000,
  "expectedSalary": 1800000,
  "noticePeriodDays": 30,
  "coverLetter": "..."
}
```

Response: `{"id": "<candidate id>", "message": "Application received"}`.

Re-applying with the same `email` to the same opening **updates** the existing candidate (so refreshes don't dupe).

Note: `POST /uploads` requires auth. If you want the careers page to accept resume uploads without making the visitor sign up first, either (a) host the resume URL externally (e.g. user enters a Google Drive link), or (b) add a separate public upload endpoint in a future phase.

### 7.3 Public Offer Accept

**No auth required.** Used by the link in the offer email.

```
GET  /public/offers/{token}                        — preview (read-only)
POST /public/offers/{token}/accept                 body: {"note": ""}
POST /public/offers/{token}/reject                 body: {"note": ""}
```

Preview returns a sanitized view:
```json
{
  "candidateName": "...",
  "candidateEmail": "...",
  "position": "...",
  "annualCtc": 1800000,
  "joiningDate": "2026-06-01",
  "validUntil": "2026-05-25",
  "notes": "...",
  "salaryBreakdown": null,
  "company": "4SightAI",
  "status": "SENT"
}
```

The token is generated server-side when HR calls `POST /hr/offers/{id}/send`. Set `OFFER_ACCEPT_URL_TEMPLATE` on the backend to your frontend's accept page URL, e.g. `https://app.example.com/offers/accept?token={token}`.

Accepting past `validUntil` returns 410.

### 7.4 SSE

```
GET /sse/notifications              (auth required — Bearer header)
```

Returns a `text/event-stream` of JSON events. Events:
- `event: notification` — payload is a notification doc
- `: heartbeat` comment lines every 20 s

Browser example:
```js
// Standard EventSource doesn't support Authorization header. Two options:
// 1) Use a polyfill like event-source-polyfill that does:
import { EventSourcePolyfill } from "event-source-polyfill";
const es = new EventSourcePolyfill("/sse/notifications", {
  headers: { Authorization: `Bearer ${token}` },
});
es.addEventListener("notification", (e) => {
  const n = JSON.parse(e.data);
  console.log("new notification", n);
});

// 2) Or ask backend to accept ?access_token= in a future tweak
```

Mobile clients (Flutter `dio`/`http`, native iOS/Android) can pass the `Authorization` header normally.

Use cases:
- Update the bell unread-count without polling
- Drop a toast in-app on new approvals / task assignments
- Refresh the inbox screen if it's open

---

## 8. Environment Variables

These exist on the backend; UI doesn't read them, but the team should know what they do.

| Var | Default | Affects UI behavior how? |
|---|---|---|
| `COMPANY_NAME` | `Your Company` | Branding in emails, payslips, offers |
| `PUBLIC_BASE_URL` | empty | When set, `/uploads` returns absolute URLs |
| `PASSWORD_RESET_URL_TEMPLATE` | empty | Welcome/password-reset emails link here |
| `OFFER_ACCEPT_URL_TEMPLATE` | empty | Offer emails embed this with `{token}` substituted |
| `REQUIRE_LOGIN_OTP` | `false` | When `true`, `/auth/login` requires OTP step |
| `OTP_TTL_MINUTES` | `10` | OTP code lifetime |
| `LATE_AFTER_HOUR` | `10` | Hour-of-day cutoff for "late" |
| `LATE_AFTER_MINUTE` | `15` | Minute cutoff |
| `GRACE_MINUTES` | `15` | Grace after the cutoff |
| `HALF_DAY_MIN_HOURS` | `4.5` | Below this → HALF_DAY |
| `OVERTIME_AFTER_HOURS` | `9` | Above this → overtime |
| `WEEKEND_DAYS` | `5,6` | Mon=0..Sun=6; default Sat+Sun |
| `MAX_UPLOAD_BYTES` | 20971520 (20 MB) | Hard cap on `/uploads` |
| `UPLOAD_DIR` | `backend/uploads` | Where files are stored locally |
| SMTP `*` | empty | Email features silently no-op when unset |

---

## 9. Common Patterns

### 9.1 Manager vs HR endpoints

Whenever both exist (`/manager/X` and `/hr/X`):
- `/manager/X` — scoped to the manager's direct reports; HR can also call it (acts on their reports, usually none)
- `/hr/X` — sees everyone

UI rule: if `user.role === "HR"`, prefer `/hr/X`. If `user.role === "MANAGER"`, use `/manager/X`. If `user.role === "USER"`, neither is exposed.

### 9.2 Field-clearing on PUT updates

| Goal | Send |
|---|---|
| Leave field unchanged | Omit it |
| Clear an optional scalar | `""` (empty string) |
| Clear an optional nested object | (not currently supported — patch with omitted fields instead) |
| Replace an array | Send the full new array |

### 9.3 Date / datetime formats

- Date strings: `YYYY-MM-DD` (no timezone)
- Datetime strings: ISO 8601 with offset (`2026-05-11T09:30:00+00:00`) — UI should treat returned datetimes as UTC unless offset says otherwise.
- `weekStart` for timesheets MUST be a Monday.

### 9.4 Pagination

Several endpoints support `?limit=` (default 50–100 depending on endpoint) and `?before=<ISO timestamp>` for cursor pagination. Tasks, todos, notifications, audit logs use this pattern.

### 9.5 Error response shape

Unchanged from baseline:
```json
{"detail": "Human-readable message"}
```

For Pydantic validation failures, FastAPI returns 422 with a structured `detail` array — show the first message to the user.

---

## 10. Smoke Test

After integrating, here's a flow that touches most of the new surface:

1. **Bootstrap**: existing HR creates a `Department` (`POST /hr/departments`).
2. HR creates a `MANAGER` user (`POST /hr/users` with `role: "MANAGER"`, `departmentId: ...`).
3. HR creates an `Employee` user with `reportingManagerId: <that-manager-id>`.
4. Employee logs in, hits `GET /dashboard/me` — expects empty-ish but valid response.
5. Employee `POST /attendance/checkin` → response includes `isLate`.
6. Employee `POST /leaves/request` → manager receives in-app notification.
7. Manager `GET /manager/leave-requests` → sees the request.
8. Manager `POST /manager/leave-requests/{id}/decide` → employee gets push + in-app + email.
9. Employee `POST /uploads` → gets back a URL.
10. Employee `POST /me/documents` using that URL → appears in HR's `GET /hr/users/{userId}/documents`.
11. Employee `POST /expenses/reimbursements` → flows through Manager → HR.
12. Manager `POST /manager/goals` for the employee → employee sees in `GET /goals/mine`.
13. HR `POST /hr/job-openings` → visit `GET /careers/openings` from a browser, no auth.
14. HR `POST /hr/offers/{id}/send` → check candidate inbox for the offer link → `GET /public/offers/{token}` → `POST /public/offers/{token}/accept`.
15. Open `GET /sse/notifications` connection as HR; trigger any action above → see the event arrive live.

---

## Questions?

Ping the backend team. Common confusions worth pre-empting:

- **"Why are my SSE events not arriving in the browser?"** EventSource doesn't support custom headers. Use a polyfill (`event-source-polyfill`) or run SSE through a service-worker proxy that adds the Authorization header.
- **"How do I send `usualWorkLocation` for a day I want to clear?"** Send `null` for that specific weekday key. To clear the whole map, omit `usualWorkLocation` from `work` and PATCH again.
- **"Half-day leave totalDays is 0.5, why?"** Server computes it from `halfDay: true` + same from/to date. The UI doesn't need to send `totalDays`.
- **"Can a manager promote another user to manager?"** No. Only HR can change `role` via `PUT /hr/users/{id}`.
- **"Where does the offer-accept link point?"** Wherever `OFFER_ACCEPT_URL_TEMPLATE` says. UI team owns the landing page that reads `?token=` and calls `GET /public/offers/{token}` then `POST /public/offers/{token}/accept`.
