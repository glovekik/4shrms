"""Standalone test for timesheet draft saving.

The bug: the app's "Done" button was a UI toggle that saved nothing, and
there was no endpoint that could have saved it — /submit demands a complete
week and sends it to a manager. So a part-filled week lived only in the
screen's state and any reload discarded it.

Runs the timesheet router via httpx ASGI transport against an in-memory fake
of db.timesheets. No live Mongo — this must never touch a real database.

Run from the backend/ directory:
    python test_timesheet_draft.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

os.environ.setdefault("SECRET_KEY", "x" * 48)
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bson import ObjectId  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import routes.timesheets as ts  # noqa: E402
from utils.dependencies import get_current_user  # noqa: E402

USER_ID = str(ObjectId())


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                # Only the operators these routes actually use.
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc.get(k) >= v["$gte"]):
                    return False
                if "$lte" in v and not (doc.get(k) is not None and doc.get(k) <= v["$lte"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(self, query: dict, *a, **kw):
        return next((d for d in self.docs if self._matches(d, query)), None)

    async def insert_one(self, doc: dict):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)

        class _R:
            inserted_id = doc["_id"]

        return _R()

    def find(self, query: dict | None = None, *a, **kw):
        docs = [d for d in self.docs if self._matches(d, query or {})]

        class _Cursor:
            def __aiter__(self_inner):
                self_inner._it = iter(docs)
                return self_inner

            async def __anext__(self_inner):
                try:
                    return next(self_inner._it)
                except StopIteration:
                    raise StopAsyncIteration

            def sort(self_inner, *a, **kw):
                return self_inner

            def limit(self_inner, *a, **kw):
                return self_inner

        return _Cursor()

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        t = next((d for d in self.docs if self._matches(d, query)), None)
        if t is None:
            return None
        for op, payload in update.items():
            if op == "$set":
                t.update(payload)
            else:
                raise NotImplementedError(op)
        return None


class FakeDB:
    def __init__(self) -> None:
        self.timesheets = FakeCollection()
        # GET /my resolves exempt days (holidays, approved leave) and builds a
        # draft from attendance. All empty here — the draft path under test
        # doesn't depend on them, and an empty week is the clean baseline.
        self.holidays = FakeCollection()
        self.leave_requests = FakeCollection()
        self.attendance = FakeCollection()
        self.users = FakeCollection()


fake_db = FakeDB()
ts.db = fake_db

app = FastAPI()
app.include_router(ts.user_router, prefix="/timesheets")
app.dependency_overrides[get_current_user] = lambda: USER_ID

# A week safely in the past, so the "no future days" rule never interferes.
WEEK = "2026-09-07"  # Monday


class T:
    passed = 0
    failed = 0

    @classmethod
    def expect(cls, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            cls.passed += 1
            print(f"  PASS  {name}")
        else:
            cls.failed += 1
            print(f"  FAIL  {name}\n        {detail}")


def stored() -> dict | None:
    return next(iter(fake_db.timesheets.docs), None)


async def run() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:

        # --- a part-filled week saves, where submit would refuse it --------
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK,
            "note": "half done",
            "entries": [
                {"date": "2026-09-07", "checkIn": "2026-09-07T09:30:00",
                 "checkOut": "2026-09-07T18:18:00", "notes": "Shipped the parser"},
                # Tuesday deliberately incomplete — no out-time, no notes.
                {"date": "2026-09-08", "checkIn": "2026-09-08T09:00:00"},
            ],
        })
        T.expect("part-filled week saves", r.status_code == 200, r.text)
        body = r.json()
        T.expect("saved as DRAFT, not PENDING",
                 body.get("status") == "DRAFT", str(body.get("status")))
        T.expect("all seven days are stored",
                 len(body.get("entries", [])) == 7,
                 str(len(body.get("entries", []))))

        mon = next(e for e in body["entries"] if e["date"] == "2026-09-07")
        T.expect("the filled day keeps its times",
                 mon["checkIn"] == "2026-09-07T09:30:00"
                 and mon["checkOut"] == "2026-09-07T18:18:00", str(mon))
        T.expect("its notes survive", mon["notes"] == "Shipped the parser", str(mon))
        T.expect("hours are derived server-side (8h48m = 8.8)",
                 abs(mon["hours"] - 8.8) < 0.01, str(mon.get("hours")))

        tue = next(e for e in body["entries"] if e["date"] == "2026-09-08")
        T.expect("the half-filled day is kept, not rejected",
                 tue["checkIn"] == "2026-09-08T09:00:00", str(tue))
        T.expect("...and scores zero hours rather than erroring",
                 tue["hours"] == 0, str(tue.get("hours")))
        T.expect("week total counts only real hours",
                 abs(body["totalHours"] - 8.8) < 0.01, str(body["totalHours"]))
        T.expect("the note is saved", body.get("note") == "half done", str(body.get("note")))

        # --- THE FIX: reading the week back returns the edits --------------
        r = await c.get(f"/timesheets/my?weekStart={WEEK}")
        T.expect("reload 200", r.status_code == 200, r.text)
        back = r.json()
        again = next(e for e in back["entries"] if e["date"] == "2026-09-07")
        T.expect("reload returns the saved draft, not a fresh one",
                 again.get("notes") == "Shipped the parser", str(again))
        T.expect("reload keeps the times",
                 again.get("checkOut") == "2026-09-07T18:18:00", str(again))
        T.expect("reloaded week is still editable (DRAFT)",
                 back.get("status") == "DRAFT", str(back.get("status")))

        # --- saving again updates in place ---------------------------------
        before = len(fake_db.timesheets.docs)
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK,
            "entries": [
                {"date": "2026-09-07", "checkIn": "2026-09-07T09:30:00",
                 "checkOut": "2026-09-07T18:18:00", "notes": "Edited again"},
            ],
        })
        T.expect("re-saving succeeds", r.status_code == 200, r.text)
        T.expect("re-saving updates rather than duplicating",
                 len(fake_db.timesheets.docs) == before,
                 str(len(fake_db.timesheets.docs)))
        mon = next(e for e in r.json()["entries"] if e["date"] == "2026-09-07")
        T.expect("the newer text wins", mon["notes"] == "Edited again", str(mon))

        # --- a week already with the manager is protected ------------------
        stored()["status"] = "PENDING"
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK,
            "entries": [{"date": "2026-09-07", "notes": "sneaky edit"}],
        })
        T.expect("can't overwrite a PENDING week", r.status_code == 400, r.text)
        T.expect("...and it says how to proceed",
                 "Withdraw" in r.json().get("detail", ""), r.text)
        T.expect("the pending sheet is untouched",
                 next(e for e in stored()["entries"]
                      if e["date"] == "2026-09-07")["notes"] == "Edited again")

        stored()["status"] = "APPROVED"
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK, "entries": [{"date": "2026-09-07", "notes": "no"}],
        })
        T.expect("can't overwrite an APPROVED week", r.status_code == 400, r.text)

        # A manager-rejected week must be editable again.
        stored()["status"] = "REJECTED"
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK,
            "entries": [{"date": "2026-09-07", "checkIn": "2026-09-07T10:00:00",
                         "checkOut": "2026-09-07T19:00:00", "notes": "Fixed"}],
        })
        T.expect("a REJECTED week can be redrafted", r.status_code == 200, r.text)

        # --- guards that still apply ---------------------------------------
        r = await c.post("/timesheets/my/draft", json={
            "weekStart": WEEK,
            "entries": [{"date": "2026-09-20", "notes": "wrong week"}],
        })
        T.expect("a date outside the week is rejected", r.status_code == 400, r.text)

        r = await c.post("/timesheets/my/draft", json={
            "weekStart": "2099-01-05",
            "entries": [{"date": "2099-01-05", "checkIn": "2099-01-05T09:00:00",
                         "notes": "time travel"}],
        })
        T.expect("logging a future day is rejected", r.status_code == 400, r.text)

    print(f"\n{T.passed} passed, {T.failed} failed")
    sys.exit(1 if T.failed else 0)


asyncio.run(run())
