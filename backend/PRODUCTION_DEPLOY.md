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
