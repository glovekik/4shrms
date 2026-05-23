# UI Integration Guide

**Backend:** FastAPI + Motor (async MongoDB) + JWT auth + APScheduler.
**Base URL (dev):** `http://localhost:8000`  ·  **Swagger UI:** `http://localhost:8000/docs`

This guide covers all endpoints in all modules. Use it as the canonical reference for integrating the UI.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Conventions](#2-conventions)
3. [Roles & Screen Routing](#3-roles--screen-routing)
4. [Module Reference](#4-module-reference)
   - [Auth](#41-auth)
   - [Attendance](#42-attendance) (incl. correction requests)
   - [Tasks](#43-tasks) (incl. comments)
   - [Teams (TL)](#44-teams-tl)
   - [Chat](#45-chat) (office + team)
   - [Leave Management](#46-leave-management)
   - [Asset Management](#47-asset-management)
   - [Office Expenses (HR)](#48-office-expenses-hr)
   - [Payroll](#49-payroll) (structure, runs, payslips, PDF, email)
   - [Onboarding](#410-onboarding)
   - [Exit Management](#411-exit-management) (incl. F&F, experience letter)
   - [HR User & Team Admin](#412-hr-user--team-admin)
5. [Cron Jobs](#5-cron-jobs)
6. [Environment Variables](#6-environment-variables)
7. [Common Patterns](#7-common-patterns)
8. [Error Reference](#8-error-reference)
9. [Smoke-Test Checklist](#9-smoke-test-checklist)

---

## 1. Quick Start

```text
1. Bootstrap: POST /auth/signup → first signup becomes role=HR
2. Login:     POST /auth/login → store access_token
3. Profile:   GET /auth/me → use role + ledTeamIds + memberOfTeamIds for nav
```

**All requests except `/auth/signup` and `/auth/login` need:**
```
Authorization: Bearer <access_token>
```
Token TTL is **24 hours** — re-prompt login on 401.

---

## 2. Conventions

- All bodies and responses are **JSON**.
- **Date strings** → `YYYY-MM-DD` (always send the user's local date).
- **Datetime strings** → ISO 8601 — sent like `"2026-05-09T09:30:00"`, returned like `"2026-05-09T09:30:00+00:00"` (UTC).
- **Money** is always INR, returned as a `number` (not formatted string).
- **Errors** are `4xx` / `5xx` with body `{ "detail": "<message>" }`.
- Most list endpoints sort newest-first by default.

### Common enums

| Enum | Values |
|---|---|
| `role` | `HR` \| `USER` |
| `tag` (job designation) | `Employee` \| `Consultant` \| `Intern` \| `Manager` \| `HR` \| `Founder` \| `CEO` |
| `status` (user) | `Active` \| `Inactive` \| `OnLeave` \| `Terminated` |
| `attendanceType` | `OFFICE` \| `WFH` \| `LEAVE` \| `HOLIDAY` |
| attendance `status` | `CHECKED_IN` \| `COMPLETED` |
| task `status` | `PENDING` \| `COMPLETED` |
| leave request `status` | `PENDING` \| `APPROVED` \| `REJECTED` \| `CANCELLED` |
| asset `status` | `AVAILABLE` \| `ASSIGNED` \| `DAMAGED` \| `LOST` \| `RETIRED` |
| asset report `status` | `PENDING` \| `RESOLVED` \| `REJECTED` |
| correction request `status` | `PENDING` \| `APPROVED` \| `REJECTED` |
| payroll run `status` | `DRAFT` \| `PROCESSED` \| `LOCKED` |
| payslip `status` | `GENERATED` \| `OVERRIDDEN` |
| onboarding `status` | `PENDING` \| `IN_PROGRESS` \| `COMPLETED` |
| exit `status` | `REQUESTED` \| `APPROVED` \| `IN_PROGRESS` \| `COMPLETED` \| `REJECTED` |
| F&F `status` | `DRAFT` \| `FINALIZED` \| `PAID` |

### Standard error shape
```json
{ "detail": "Descriptive message" }
```

### Permission model
- **`role: HR`** — can hit all `/hr/...` endpoints.
- **`role: USER`** — user endpoints only. Cannot hit HR endpoints (returns 403).
- **TL** is *not* a global role — it's whoever is set as `teamLeadId` on a given team. Use `ledTeamIds` from `/auth/me` to know if the user leads any team.

---

## 3. Roles & Screen Routing

After login, call `GET /auth/me` and use the response to drive navigation:

```js
const me = await fetch('/auth/me', { headers: authHeaders }).then(r => r.json());

// Tabs to show:
const showHRDashboard = me.role === 'HR';
const showTLTab       = me.ledTeamIds.length > 0;
const showMyTeamsTab  = me.memberOfTeamIds.length > 0;

// Profile header always shows:
const { name, email, employeeCode, tag, status, profilePictureUrl, joiningDate } = me;
```

`/auth/me` shape:
```json
{
  "id": "67ccc...",
  "name": "Bob",
  "email": "bob@x.com",
  "role": "USER",
  "tag": "Manager",
  "employeeCode": "EMP-0042",
  "workPhone": "+91-9876543210",
  "joiningDate": "2026-01-15",
  "status": "Active",
  "profilePictureUrl": "https://cdn.example.com/photo.png",
  "ledTeamIds":      ["67team1..."],
  "memberOfTeamIds": ["67team2...", "67team3..."]
}
```

---

## 4. Module Reference

### 4.1 Auth

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/signup` | public | First signup → HR. Subsequent → 403 (HR creates users). |
| POST | `/auth/login` | public | Returns `{ access_token, token_type }`. |
| GET | `/auth/me` | bearer | Returns full profile + team memberships. |

#### `POST /auth/signup`
Req: `{ "name", "email", "password" }` → Res: `{ "message", "userId", "role": "HR" }`
Errors: `400 Email already exists` · `403 Signup is closed. Contact HR to be added.`

#### `POST /auth/login`
Req: `{ "email", "password" }` → Res: `{ "access_token", "token_type": "bearer" }`
Errors: `400 Invalid email or password`

---

### 4.2 Attendance

#### Daily flow (any user)

| Method | Path | Notes |
|---|---|---|
| POST | `/attendance/checkin` | `{ date, attendanceType }` |
| POST | `/attendance/checkout` | `{ date, workNotes }` (workNotes required, non-empty) |
| GET | `/attendance/today?date=YYYY-MM-DD` | Pass user's local date so it matches `/checkin` |
| GET | `/attendance/history` | All caller's records, newest first |
| PUT | `/attendance/update/{id}` | `{ attendanceType, workNotes?, checkIn?, checkOut? }` — partial; status auto-recomputed |
| DELETE | `/attendance/delete/{id}` | — |

**Today/History response includes** `autoClosedByCron: bool` — show a warning badge when true (means the user forgot to checkout and the midnight cron auto-closed it).

#### Correction request flow (auto-closed records)

| Method | Path | Auth | Body |
|---|---|---|---|
| POST | `/attendance/{id}/correction-request` | own | `{ requestedCheckOut, reason }` |
| GET | `/attendance/correction-requests/mine?status=PENDING` | user | — |
| GET | `/hr/correction-requests?status=PENDING` | HR | — |
| POST | `/hr/correction-requests/{id}/decide` | HR | `{ action: "APPROVE"\|"REJECT", note?, overrideCheckOut? }` |

On HR APPROVE: `attendance.checkOut` is updated and `autoClosedByCron` flag is removed.

---

### 4.3 Tasks

#### User endpoints

| Method | Path | Body |
|---|---|---|
| GET | `/tasks/my?status=PENDING` | — |
| GET | `/tasks/{id}` | — (returns task with `assignee` + `createdByUser` populated) |
| POST | `/tasks/{id}/complete` | — (the checkbox; auto-creates today's attendance if missing) |
| POST | `/tasks/{id}/uncomplete` | — (undo; reverses attendance changes) |

**Access for `GET /tasks/{id}`:** assignee, the team's TL, or HR.

#### Comments thread

| Method | Path | Body |
|---|---|---|
| GET | `/tasks/{id}/comments` | — (oldest first, with `user` populated on each) |
| POST | `/tasks/{id}/comments` | `{ text }` |
| DELETE | `/tasks/{id}/comments/{commentId}` | — (own only) |

Comment shape:
```json
{ "id", "taskId", "userId", "user": { "id", "name", "email" }, "text", "createdAt" }
```

#### Checkbox semantics
`POST /tasks/{id}/complete`:
1. Sets task `status=COMPLETED`, `completedAt=now`.
2. Finds today's attendance — if missing, auto-creates `{ attendanceType: "OFFICE", status: "CHECKED_IN", checkIn: now }`.
3. Adds task ID to `attendance.completedTasks[]` and appends `- <task title>` to `attendance.workNotes`.

`POST /tasks/{id}/uncomplete` reverses #1 and #3 (#2 is left alone — the attendance row stays).

---

### 4.4 Teams (TL)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/tl/teams/mine` | — | Returns full Team objects with **populated `members[]` + `teamLead`**. Empty array if caller leads nothing. |
| POST | `/tl/teams/{teamId}/tasks` | `{ title, description?, assigneeId, reminderIntervalMinutes?, dueDate? }` | Assignee must be a team member or the TL. |
| GET | `/tl/teams/{teamId}/tasks` | — | Each task includes **populated `assignee`**. |
| PUT | `/tl/tasks/{id}` | partial | — |
| DELETE | `/tl/tasks/{id}` | — | — |

`/tl/teams/mine` response:
```json
[
  {
    "id": "67team1...", "name": "Backend Squad",
    "teamLeadId": "67ccc...",
    "memberIds":   ["67ddd...", "67eee..."],
    "members":     [{ "id", "name", "email" }, ...],
    "teamLead":    { "id", "name", "email" }
  }
]
```

> **Reminder semantics:** `reminderIntervalMinutes` is **stored config only**. Backend doesn't push. The mobile/web client reads it from `/tasks/my` and runs a local timer / notification.

---

### 4.5 Chat

Two channels — same shape, different scope. Polling-based (no WebSocket).

| Method | Path | Auth | Body |
|---|---|---|---|
| GET | `/office/messages?before=<iso>&limit=50` | any user | — |
| POST | `/office/messages` | any user | `{ text }` |
| DELETE | `/office/messages/{messageId}` | author | — |
| GET | `/teams/{teamId}/messages?before=<iso>&limit=50` | member, TL, or HR | — |
| POST | `/teams/{teamId}/messages` | member, TL, or HR | `{ text }` |
| DELETE | `/teams/{teamId}/messages/{messageId}` | author | — |

**Pagination:** GET returns up to `limit` newest messages (max 100), reversed to **oldest-first within the page** — UI just appends to the bottom. To load older, pass `?before=<createdAt of your oldest visible message>`. Returns `[]` when nothing older.

**Polling:** for live tail, call without `?before=` every ~3s. Optionally short-circuit by comparing the newest message ID.

Message shape:
```json
{
  "id": "67msg...",
  "userId": "67ccc...",
  "user": { "id", "name", "email" },
  "text": "Hey team",
  "createdAt": "2026-05-09T04:10:00+00:00"
}
```

---

### 4.6 Leave Management

#### HR setup (do this first or no leaves work)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/leave-types` | `{ code, name, daysPerMonth, daysPerYear, allowHalfDay, requiresAttachment, description?, isActive }` |
| GET | `/hr/leave-types` | all (incl. inactive) |
| PUT | `/hr/leave-types/{id}` | partial |
| DELETE | `/hr/leave-types/{id}` | (or set `isActive: false` to hide without deleting history) |

Suggested seed: `EARNED` (1/month, 12/year), `SICK` (0.5/month, 6/year), `MATERNITY` (0/0 — HR allocates manually).

#### User self-service (`/leaves/...`)

| Method | Path | Body |
|---|---|---|
| GET | `/leaves/types` | — (only active types) |
| GET | `/leaves/balance` | — (always returns one row per active type, with `remaining = allocated - used - pending`) |
| POST | `/leaves/request` | `{ leaveTypeCode, fromDate, toDate, reason, halfDay?, halfDayPart?: "FIRST"\|"SECOND", attachmentUrl? }` |
| GET | `/leaves/mine?status=PENDING` | — |
| POST | `/leaves/{id}/cancel` | — (PENDING only) |

**Half-day:** `halfDay: true`, `halfDayPart: "FIRST"\|"SECOND"`, `fromDate === toDate`. Counts as 0.5 days.

**`totalDays`** is computed inclusive: `(toDate - fromDate).days + 1` (or `0.5` for half-day).

**Server enforces:** type exists & active → half-day allowed if requested → attachment required (per type config) → reason non-empty → no overlap with existing PENDING/APPROVED → balance available.

#### HR review

| Method | Path | Body |
|---|---|---|
| GET | `/hr/leave-requests?status=PENDING` | each item populated with `user` + `leaveType` |
| POST | `/hr/leave-requests/{id}/decide` | `{ action: "APPROVE"\|"REJECT", note? }` |
| GET | `/hr/users/{userId}/leave-balance?year=2026` | view any user's balances |

Balance shape:
```json
{ "leaveTypeCode", "leaveType": {...}, "year", "allocated", "used", "pending", "remaining" }
```

---

### 4.7 Asset Management

#### HR (`/hr/...`)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/assets` | `{ code, name, category, serialNumber?, notes?, purchaseDate?, purchasePrice? }` |
| GET | `/hr/assets?status=&assignedToUserId=&category=` | — |
| GET | `/hr/assets/{id}` | — |
| PUT | `/hr/assets/{id}` | partial (incl. `status` override) |
| DELETE | `/hr/assets/{id}` | — |
| POST | `/hr/assets/{id}/assign` | `{ userId, notes? }` (asset must be AVAILABLE) |
| POST | `/hr/assets/{id}/return` | `{ notes?, status?: "AVAILABLE"\|"DAMAGED"\|"LOST" }` |
| GET | `/hr/asset-reports?status=PENDING` | with `asset` + `reporter` populated |
| POST | `/hr/asset-reports/{id}/resolve` | `{ action, resolution?, newAssetStatus? }` |

#### User (`/assets/...`)

| Method | Path | Body |
|---|---|---|
| GET | `/assets/mine` | — |
| POST | `/assets/{id}/report-issue` | `{ reportType: "DAMAGE"\|"LOSS"\|"OTHER", description }` |

---

### 4.8 Office Expenses (HR)

All under `/hr/expenses`.

| Method | Path | Body / Query |
|---|---|---|
| POST | `/hr/expenses` | `{ title, amount, category, date, description?, receiptUrl?, vendor?, paymentMethod? }` |
| GET | `/hr/expenses?from=2026-05-01&to=2026-05-31&category=` | — |
| GET | `/hr/expenses/summary?year=2026&month=5` | totals + by-category breakdown |
| GET | `/hr/expenses/{id}` | — |
| PUT | `/hr/expenses/{id}` | partial |
| DELETE | `/hr/expenses/{id}` | — |

Summary response:
```json
{
  "year": 2026, "month": 5, "totalAmount": 23700,
  "byCategory": [
    { "category": "RENT", "total": 15000, "count": 1 },
    ...
  ]
}
```

---

### 4.9 Payroll

Workflow: **HR sets salary structure → creates a payroll run → processes → optionally overrides individual payslips → locks → emails** (or users download).

#### Salary structure (HR)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/users/{userId}/salary-structure` | full structure (see below) — closes previous, opens new for revision history |
| GET | `/hr/users/{userId}/salary-structure` | current active |
| GET | `/hr/users/{userId}/salary-history` | all versions, newest first |

Body for POST:
```json
{
  "basic": 30000, "hra": 12000, "communicationAllowance": 1500, "otherAllowance": 5000,
  "employerInsurance": 800,
  "professionalTax": 200, "tds": 1500, "employeeInsurance": 400,
  "employerPF": null, "employeePF": null,
  "panNumber": "ABCDE1234F", "uanNumber": "100123456789",
  "bankAccountNumber": "1234567890", "bankIfsc": "HDFC0001234", "bankName": "HDFC",
  "tdsRegime": "NEW"
}
```
Setting PF fields to `null` triggers **auto-PF**: `min(basic, 15000) × 12%` (max ₹1800) per side.

#### Payroll runs (HR)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/payroll/runs` | `{ year, month, workingDays }` (default 22) |
| GET | `/hr/payroll/runs` | — |
| GET | `/hr/payroll/runs/{id}` | — |
| DELETE | `/hr/payroll/runs/{id}` | DRAFT only |
| POST | `/hr/payroll/runs/{id}/process` | generates payslips for all Active users with structures. Returns `{ generated, skipped }`. Re-running re-processes. |
| POST | `/hr/payroll/runs/{id}/lock` | PROCESSED → LOCKED |

#### Payslips

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/hr/payroll/runs/{id}/payslips` | HR | with `user` populated |
| GET | `/hr/payslips/{id}` | HR | — |
| PUT | `/hr/payslips/{id}` | HR | partial override; recomputes totals; **invalidates cached PDF** |
| GET | `/hr/payslips/{id}/pdf` | HR | downloads PDF |
| POST | `/hr/payslips/{id}/email` | HR | emails to recipient (503 if SMTP not configured) |
| POST | `/hr/payroll/runs/{id}/email-all` | HR | bulk email; returns `{ sentCount, failedCount, skippedCount, sent[], failed[], skipped[] }` |
| GET | `/payroll/payslips` | user | own history |
| GET | `/payroll/payslips/{id}` | user | own only |
| GET | `/payroll/payslips/{id}/pdf` | user | own PDF |

#### Calculation rules (locked in code)
- **PF** auto-computed when null → `min(basic, 15000) × 12%`, max ₹1800.
- **TDS / PT / Insurance** are HR-entered monthly amounts.
- **LOP days** = `workingDays - (count of attendance records that month)`.
- **LOP deduction** = `(monthlyGross / workingDays) × lopDays`, subtracted from gross.
- **Total Gross** = Basic + HRA + Comm + Other + EmployerPF + EmployerInsurance.
- **Total Deductions** = EmployeePF + PT + TDS + EmployeeInsurance.
- **Net Pay** = Gross (after LOP) − Deductions.

#### Payslip response shape
```json
{
  "id": "67pay...", "payrollRunId": "67run...", "userId": "67ccc...",
  "user": { "id", "name", "email", "employeeCode" },
  "year": 2026, "month": 5,
  "basic": 30000, "hra": 12000, "communicationAllowance": 1500, "otherAllowance": 5000,
  "employerPF": 1800, "employerInsurance": 800,
  "employeePF": 1800, "professionalTax": 200, "tds": 1500, "employeeInsurance": 400,
  "panNumber", "uanNumber", "bankAccountNumber", "bankIfsc", "bankName", "tdsRegime",
  "workingDays": 22, "attendedDays": 21, "lopDays": 1, "lopDeduction": 2322.73,
  "totalGross": 48777.27, "totalDeductions": 3900, "netPay": 44877.27,
  "status": "GENERATED", "notes": "",
  "generatedAt": "2026-06-01T03:15:00+00:00"
}
```

#### PDF download
- Response is `application/pdf` with `Content-Disposition: attachment; filename="Payslip_<Name>_<Month>_<Year>.pdf"`.
- Generated once via reportlab, cached in MongoDB GridFS (bucket `payslip_pdfs`).
- HR's `PUT /hr/payslips/{id}` invalidates the cache; next download regenerates.

---

### 4.10 Onboarding

`onboardings` collection — one per user. Three sub-array checklists with item IDs (uuid).

#### Default checklists (auto-populated on creation)
- **Documents:** Aadhaar, PAN, Photo, Educational Certs, Prev Employment Letter, Address Proof, Bank Cheque
- **HR tasks:** Create email, Add to Slack, Send welcome email, Assign laptop, Add to HRMS, Schedule orientation
- **Employee tasks:** Bank details, Submit docs, Complete profile, Read handbook, Sign NDA

#### HR (`/hr/onboardings`)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/onboardings` | `{ userId }` |
| GET | `/hr/onboardings?status=IN_PROGRESS` | — |
| GET | `/hr/onboardings/{id}` | — |
| POST | `/hr/onboardings/{id}/welcome-email` | — |
| PUT | `/hr/onboardings/{id}/document-status` | `{ documentId, status: "VERIFIED"\|"REJECTED"\|..., note? }` |
| PUT | `/hr/onboardings/{id}/hr-task-status` | `{ taskId, status: "DONE"\|"PENDING", note? }` |
| POST | `/hr/onboardings/{id}/complete` | — |
| DELETE | `/hr/onboardings/{id}` | — |

#### User (`/onboarding`)

| Method | Path | Body |
|---|---|---|
| GET | `/onboarding/mine` | — (returns `null` if HR hasn't created one) |
| POST | `/onboarding/document-upload` | `{ documentId, fileUrl }` (frontend uploads to storage, sends URL) |
| PUT | `/onboarding/employee-task-status` | `{ taskId, status, note? }` |

Onboarding response:
```json
{
  "id", "userId", "status",
  "documents": [{ "id", "title", "required", "status", "fileUrl", "note", "uploadedAt", "verifiedAt", "verifiedBy" }],
  "hrTasks":      [{ "id", "title", "status", "note", "completedAt", "completedBy" }],
  "employeeTasks":[{ "id", "title", "status", "note", "completedAt" }],
  "welcomeEmailSent", "welcomeEmailSentAt",
  "startedAt", "completedAt"
}
```

---

### 4.11 Exit Management

#### Lifecycle
1. Employee `POST /exit/resign` → status `REQUESTED`.
2. HR `POST /hr/exits/{id}/decide` with `APPROVE` → `APPROVED`.
3. HR ticks tasks — first DONE bumps to `IN_PROGRESS`.
4. HR fills/finalizes/marks-paid F&F.
5. HR issues experience letter PDF.
6. HR `POST /hr/exits/{id}/complete` → `COMPLETED`. **User auto-flipped to `Terminated`.** Refuses if any assets are still ASSIGNED.

#### User (`/exit`)

| Method | Path | Body |
|---|---|---|
| POST | `/exit/resign` | `{ requestedLastWorkingDay, reason }` |
| GET | `/exit/mine` | — |
| PUT | `/exit/employee-task-status` | `{ taskId, status, note? }` |
| GET | `/exit/experience-letter` | downloads PDF (404 until issued) |

#### HR (`/hr/exits`)

| Method | Path | Body |
|---|---|---|
| GET | `/hr/exits?status=APPROVED` | — |
| GET | `/hr/exits/{id}` | — |
| POST | `/hr/exits/{id}/decide` | `{ action: "APPROVE"\|"REJECT", approvedLastWorkingDay?, note? }` (`approvedLastWorkingDay` required on APPROVE) |
| PUT | `/hr/exits/{id}/hr-task-status` | `{ taskId, status, note? }` |
| PUT | `/hr/exits/{id}/ffs` | `{ pendingSalary?, leaveEncashment?, bonus?, deductions?, notes? }` (server recomputes `totalPayable`) |
| POST | `/hr/exits/{id}/ffs/finalize` | DRAFT → FINALIZED |
| POST | `/hr/exits/{id}/ffs/mark-paid` | FINALIZED → PAID |
| POST | `/hr/exits/{id}/experience-letter` | generates + caches PDF (replaces previous) |
| GET | `/hr/exits/{id}/experience-letter` | downloads cached PDF |
| POST | `/hr/exits/{id}/complete` | finalize exit |

F&F sub-doc shape (inside `exit.ffsCalculation`):
```json
{
  "pendingSalary": 25000, "leaveEncashment": 8000,
  "bonus": 5000, "deductions": 1200,
  "totalPayable": 36800,
  "status": "FINALIZED", "notes": "...",
  "finalizedAt": "...", "paidAt": null
}
```

---

### 4.12 HR User & Team Admin

#### Users (`/hr/users`)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/users` | `{ name, email, password, tag?, employeeCode?, workPhone?, joiningDate?, status?, profilePictureUrl? }` |
| GET | `/hr/users` | — |
| GET | `/hr/users/{id}` | — |
| PUT | `/hr/users/{id}` | partial — any of `name, tag, employeeCode, workPhone, joiningDate, status, profilePictureUrl` |

> `email`, `password`, `role` intentionally NOT editable here. Role promotion is out-of-band (backend script `promote_hr.py`).

#### Teams (`/hr/teams`)

| Method | Path | Body |
|---|---|---|
| POST | `/hr/teams` | `{ name, teamLeadId, memberIds: [] }` (TL is *not* automatically a member) |
| GET | `/hr/teams` | — |
| GET | `/hr/teams/{id}` | — |
| PUT | `/hr/teams/{id}` | partial |
| DELETE | `/hr/teams/{id}` | — |

---

## 5. Cron Jobs

Two background jobs run via APScheduler in-process — no UI involvement, just FYI:

| Job | Schedule | What |
|---|---|---|
| `auto_close_attendance` | Daily at **00:01 server local time** | Any `CHECKED_IN` attendance from a prior date → `checkOut=23:59:59`, `status=COMPLETED`, `autoClosedByCron=true`. |
| `monthly_leave_accrual` | 1st of each month at **00:05** | For each Active user × each active leave type, `balance.allocated += daysPerMonth` (capped at `daysPerYear`). |

After auto-close, the user can `POST /attendance/{id}/correction-request` to fix the time.

---

## 6. Environment Variables

The backend reads these on startup. UI doesn't need to set them, but knowing they exist helps debug:

| Var | Default | Used by |
|---|---|---|
| `SECRET_KEY` | hardcoded fallback (dev) | JWT signing — set this in prod |
| `COMPANY_NAME` | `"Your Company"` | Payslip header, welcome/payroll emails |
| `COMPANY_ADDRESS` | `""` | Payslip header |
| `COMPANY_LOGO_PATH` | `""` | Local file path to logo PNG/JPG (payslip + experience letter) |
| `SMTP_HOST` | `""` | Email features — without this, email endpoints return `503 Email is not configured` |
| `SMTP_PORT` | `587` | — |
| `SMTP_USERNAME` | `""` | — |
| `SMTP_PASSWORD` | `""` | — |
| `SMTP_FROM` | `""` | — |
| `SMTP_USE_TLS` | `true` | — |

If you need email-related features to work in dev, set `SMTP_HOST` and `SMTP_FROM` (Gmail app password works fine).

---

## 7. Common Patterns

### Populated user/asset/team objects
Many list endpoints return both an ID *and* a populated object so the UI doesn't need a follow-up call:
- `/tl/teams/mine` → `members[]` + `teamLead`
- `/tl/teams/{teamId}/tasks` → each task has `assignee`
- `/hr/leave-requests` → each item has `user` + `leaveType`
- `/hr/correction-requests` → each item has `user` + `attendance` summary
- `/hr/asset-reports` → each item has `asset` + `reporter`
- `/hr/payroll/runs/{id}/payslips` → each payslip has `user`
- Comments / chat messages → each has `user`

When the populated object is missing (e.g. user was deleted), the field is `null` — render a fallback.

### Polling (chat)
Default interval: 3 seconds. To "load more" history, pass `?before=<oldest createdAt you have>`. Returns `[]` when there's nothing older.

### Optimistic insert pattern (comments + chat)
On POST, render the user's text immediately, then replace the optimistic row with the server response (which has the real `id` and `createdAt`). On error, surface inline.

### File uploads
The backend doesn't handle multipart uploads. Frontend uploads files to your storage of choice (S3, blob, etc.) and sends back the URL:
- Profile picture: `PUT /hr/users/{id}` with `{ profilePictureUrl: "..." }`
- Asset receipts: `POST /hr/assets` with `{ ... }` (no file field — manage externally)
- Expense receipts: `receiptUrl`
- Onboarding documents: `POST /onboarding/document-upload { documentId, fileUrl }`
- Leave attachments: `attachmentUrl` on the request

### Date handling
Always pass the user's **local date** (`YYYY-MM-DD`) for fields like `date`, `fromDate`, `toDate`, `joiningDate`. Never compute in the browser as UTC — that causes off-by-one near midnight.

### Auth header helper
```js
const authHeaders = () => ({
  'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
  'Content-Type': 'application/json',
});
```

Watch for 401s (token expired) and redirect to login.

---

## 8. Error Reference

All non-2xx responses are `{ "detail": "<message>" }`.

| Code | Meaning | Common causes |
|---|---|---|
| `400` | Validation failure | Bad JSON, unknown enum, malformed id, duplicate email/code, balance insufficient, attempting an illegal state transition |
| `401` | Missing or invalid bearer token | Token expired (24h) — re-login |
| `403` | Authenticated but not allowed | Trying HR endpoint as USER, deleting someone else's comment, modifying not-your-task, etc. |
| `404` | Resource not found | Wrong ID, deleted record, no salary structure for user |
| `502` | Upstream failed | Email send failed (SMTP error) |
| `503` | Service not configured | SMTP env vars missing when calling email endpoints |

UI should:
- 401 → silently re-prompt login.
- 403 → show "you don't have access to this".
- 503 (email-related) → show "Email isn't configured on the server. Contact admin."
- 502 → "Email send failed: <detail>". Retry-able.
- 400/404 → show `detail` directly.

---

## 9. Smoke-Test Checklist

To verify your integration covers the major flows:

- [ ] Signup → first user becomes HR
- [ ] Login → token stored
- [ ] `/auth/me` → role + team memberships render the right tabs
- [ ] HR creates a user via `/hr/users`
- [ ] HR creates a team with that user as TL
- [ ] As TL: `/tl/teams/mine` shows the team with populated `members`
- [ ] As TL: create a task with `reminderIntervalMinutes`
- [ ] As assignee: `/tasks/my` shows the task; reminder timer fires
- [ ] Check off the task → today's attendance auto-creates with the title appended to workNotes
- [ ] HR creates leave types (`EARNED`, `SICK`)
- [ ] As user: request leave; as HR: approve; balance updates
- [ ] HR creates an asset and assigns it; user sees it in `/assets/mine`
- [ ] HR creates an expense; summary endpoint reflects it
- [ ] HR sets a salary structure
- [ ] HR creates payroll run, processes, downloads payslip PDF
- [ ] HR locks the run → further overrides return 400
- [ ] HR creates onboarding for new user → user sees checklists in `/onboarding/mine`
- [ ] User uploads a doc via `/onboarding/document-upload`
- [ ] Welcome email sent (if SMTP configured)
- [ ] Employee resigns → HR approves → fills F&F → finalizes → marks paid
- [ ] HR issues experience letter → user can download via `/exit/experience-letter`
- [ ] HR completes exit → user's status flips to `Terminated`

---

## Appendix: Module index by endpoint count

| Module | Endpoint count |
|---|---|
| Auth | 3 |
| Attendance (incl. correction) | 10 |
| Tasks (incl. comments) | 7 |
| Teams (TL) | 5 |
| Chat (office + team) | 6 |
| Leave Management | 12 |
| Asset Management | 11 |
| Office Expenses | 6 |
| Payroll (incl. PDF/email) | 14 |
| Onboarding | 11 |
| Exit Management | 14 |
| HR User/Team Admin | 9 |
| **Total** | **~108** |

---

*Last updated alongside backend changes. If something here drifts from `/docs`, trust `/docs`.*
