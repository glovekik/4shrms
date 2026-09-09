# Production deploy — projects, chat groups, org chart

Order matters. Read the whole file before starting.

## 0. Required environment variables

The container gets these from the platform; `.env` is local-only and is not
copied into the image.

| Variable | Value | Notes |
|---|---|---|
| `MONGO_URL` | the production Mongo URI | no default — the app refuses to boot without it |
| `MONGO_DB_NAME` | `attendance_db` | |
| `SECRET_KEY` | **strong random string** | see below |
| `CORS_ORIGINS` | `https://hrms.4sightai.com` | optional; already the default |
| `ENABLE_DEV_CORS` | **do not set** | adds `http://localhost:*` to the allowlist — laptops only |

`SECRET_KEY` now has **no fallback**. It previously defaulted to
`attendance_secret_key`, a literal in this repo — a deploy that forgot to set
it signed forgeable tokens. The app now refuses to start if it is missing, or
if it is weak while `MONGO_URL` is non-local. Generate one with:

    python -c "import secrets; print(secrets.token_urlsafe(48))"

**Verify the current production value before deploying.** If it has been the
default all along, changing it signs out every user — do it deliberately.

## 1. Back up

The rollback script restores from a snapshot, and the one in `backups/` is
stale. Take a fresh one:

    mongodump --uri "$MONGO_URL" --db attendance_db \
      --out "backups/pre-deploy-$(date +%F)"

## 2. Migrate — dry run first

    python migrate_teams_to_projects.py          # writes nothing

It **aborts** rather than proceeding if it finds either:
  * two projects that would end up sharing a `code` (uniquely indexed), or
  * any task / timesheet / chat message / membership row still pointing at a
    project document the merge deletes.

Both abort with the offending rows listed. Fix those first, re-run the dry
run, and only then:

    python migrate_teams_to_projects.py --apply
    python migrate_chat_to_projects.py           # dry run
    python migrate_chat_to_projects.py --apply

Both are idempotent — re-running creates no duplicates.

The migration deliberately **keeps** `memberIds` / `projectManagerIds` on
project documents and copies the merged roster into them. The code currently
in production reads those fields, so leaving them means rosters keep working
in the window between migrating and deploying. They are dead to the new code;
drop them in a later cleanup once the new build is confirmed healthy.

## 3. Deploy the backend

    docker build -t hrms-api .
    # then deploy as usual

Check `/healthz` returns 200 and the startup log line `CORS origins: [...]`
lists only `https://hrms.4sightai.com` — no `localhost` entries. If any appear,
`ENABLE_DEV_CORS` or `CORS_ORIGINS` is set in the container's environment and
should be removed.

## 4. Deploy the web app

Build in a **clean shell**. `EXPO_PUBLIC_API_URL` overrides `app.json`, so a
leftover export from local testing bakes `localhost` into the production
bundle:

    unset EXPO_PUBLIC_API_URL
    npx expo export --platform web

Confirm the built output contains `hrmsapi.4sightai.com` and not `localhost`.

## 5. Post-deploy data tasks (HR)

  * create the CEO account (`promote_ceo.py`) — the CEO dashboard is Phase 6
  * assign the unassigned people to departments (they show in the org chart's
    "unassigned" bucket until then)
  * set department heads
  * retire the unused departments

## Rollback

    python rollback_phase1.py backups/pre-deploy-<date>.json

Restores `projects` and `teams` and drops every `project_members` row. Then
redeploy the previous image.

---

## Email notifications

### 1. Credentials — put these in the server `.env`

```bash
SMTP_HOST=                 # smtp.zoho.in | smtp.gmail.com | email-smtp.ap-south-1.amazonaws.com
SMTP_PORT=587              # 587 STARTTLS, 465 implicit TLS (both handled)
SMTP_USERNAME=             # usually the full mailbox address
SMTP_PASSWORD=             # app password, never the account password
SMTP_FROM=hrms@4sightai.com
SMTP_USE_TLS=true          # ignored when SMTP_PORT=465

EMAIL_FROM_NAME=4SightHub HR
EMAIL_REPLY_TO=            # a monitored inbox — replies to automated mail reach a person
APP_BASE_URL=https://hrms.4sightai.com   # buttons in emails need absolute URLs
```

Optional, with working defaults: `EMAIL_MAX_ATTEMPTS=3`,
`EMAIL_RETRY_BACKOFF_SECONDS=5`, `EMAIL_LOG_TTL_DAYS=180`.

### 2. Nothing sends until you say so

`EMAIL_ENABLED_EVENTS` is an allow-list and **defaults to empty**. This is
deliberate: fifteen code paths call the sender, and without the list, setting
`SMTP_HOST` would switch all of them on at once — welcome mails, payslips,
leave decisions, task assignments — with no chance to check deliverability on
something low-stakes first.

```bash
# Start here. Both are things people actively wait for, and neither is noisy.
EMAIL_ENABLED_EVENTS=password_reset,payslip_ready

# Then widen once delivery is proven.
EMAIL_ENABLED_EVENTS=password_reset,payslip_ready,account_created,leave_request,leave_decision,reimbursement_request,reimbursement_decision,auto_checkout,offer_sent,offer_response

# Everything, including the tier-2 events.
EMAIL_ENABLED_EVENTS=*
```

The startup log states which it is. If SMTP is configured and the list is
empty it logs a **warning** naming the variable, because "configured and
silent" is otherwise baffling.

| Event | Goes to | Fires when |
|---|---|---|
| `password_reset` | the employee | Forgot-password requested |
| `login_otp` | the employee | Login, only when `REQUIRE_LOGIN_OTP=true` |
| `account_created` | the employee | HR creates an account (carries the setup link) |
| `onboarding_welcome` | the employee | HR clicks Send welcome email |
| `payslip_ready` | the employee | HR sends a payslip — PDF attached |
| `leave_request` | manager + HR | Employee applies for leave |
| `leave_decision` | the employee | Leave approved or rejected |
| `reimbursement_request` | manager + HR | Claim submitted |
| `reimbursement_decision` | the employee | Either approval stage decides |
| `correction_decision` | the employee | Attendance correction decided |
| `auto_checkout` | the employee | Cron closes a forgotten check-out at 00:01 |
| `offer_sent` | the candidate | HR sends an offer |
| `offer_response` | the HR who sent it | Candidate accepts or declines via the public link |
| `task_assigned` | the assignee | A TL assigns a task |
| `task_complete` | task watchers | A task is marked complete |

Approver emails follow the same rule as the in-app notification — the
reporting manager (if active) plus every active HR user — and each recipient's
button points at the screen their role actually uses.

### 3. Verify before switching events on

```
POST /hr/email/test      → sends to the calling HR user, waits, returns the config
GET  /hr/email/log       → recent attempts; ?status=failed&event=payslip_ready
```

`/hr/email/test` deliberately ignores the allow-list — it answers "do these
credentials work?", which is the question you ask *before* enabling anything.

### 4. DNS

Add **SPF** and **DKIM** for the sending domain before enabling `payslip_ready`.
Without them payslips land in spam, and a payslip in spam becomes a support
ticket. SES additionally needs domain verification and a sandbox exit (~24h)
before it will send to unverified addresses.

### 5. What is logged

`email_log` records event, recipient, subject, status, attempt count and the
server's error — never the message body, because these carry payslips and
password codes. Rows expire after `EMAIL_LOG_TTL_DAYS`.
