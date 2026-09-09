"""End-to-end email test against a local aiosmtpd catcher.

Covers what unit tests can't: that a message actually reaches an SMTP
server, in the right MIME shape, with the right headers, and that the
allow-list, delivery log and retry policy behave on the wire.

Needs a local mongod (it asserts on real email_log rows) and a free port
8025. Run from backend/:
    python test_email_delivery.py
"""
import asyncio, os, sys, email

os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = "8025"
os.environ["SMTP_FROM"] = "hrms@test.local"
os.environ["SMTP_USERNAME"] = ""
os.environ["SMTP_USE_TLS"] = "false"
os.environ["EMAIL_FROM_NAME"] = "4SightHub HR"
os.environ["EMAIL_REPLY_TO"] = "people@test.local"
os.environ["APP_BASE_URL"] = "https://hrms.example.com"
os.environ["EMAIL_ENABLED_EVENTS"] = "leave_decision,payslip_ready"
os.environ["EMAIL_RETRY_BACKOFF_SECONDS"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiosmtpd.controller import Controller

inbox = []
class Handler:
    async def handle_DATA(self, server, session, envelope):
        inbox.append(email.message_from_bytes(envelope.content))
        return "250 OK"

P, F = [0], [0]
def ok(n): P[0] += 1; print(f"  PASS  {n}")
def bad(n, d=""): F[0] += 1; print(f"  FAIL  {n}\n        {d}")
def expect(n, c, d=""): ok(n) if c else bad(n, d)

ctrl = Controller(Handler(), hostname="127.0.0.1", port=8025)


async def main():
    try:
        from utils.email import send_event_email, send_email_with_pdf
        from database import db

        # --- 1. an enabled event is delivered -------------------------------
        sent = await send_event_email(
            "leave_decision", "alice@test.local",
            subject="Leave approved — 2026-09-10 to 2026-09-12",
            headline="Your leave request has been approved",
            greeting="Alice",
            rows=[("From", "2026-09-10"), ("To", "2026-09-12"),
                  ("Days", 3), ("Type", "CL"), ("Blank", "")],
            cta=("View in the app", "/leaves"),
            note="Note from HR: enjoy",
        )
        expect("enabled event accepted", sent is True, f"got {sent}")
        await asyncio.sleep(1.5)
        expect("message delivered", len(inbox) == 1, f"inbox={len(inbox)}")

        m = inbox[0]
        expect("multipart/alternative",
               m.get_content_type() == "multipart/alternative",
               m.get_content_type())
        parts = {p.get_content_type() for p in m.walk() if not p.is_multipart()}
        expect("both text and html parts",
               parts == {"text/plain", "text/html"}, str(parts))
        expect("From carries display name",
               m["From"] == "4SightHub HR <hrms@test.local>", m["From"])
        expect("Reply-To set", m["Reply-To"] == "people@test.local", m["Reply-To"])

        bodies = {p.get_content_type(): p.get_payload(decode=True).decode()
                  for p in m.walk() if not p.is_multipart()}
        text, html = bodies["text/plain"], bodies["text/html"]
        expect("text has the rows", "From:" in text and "2026-09-10" in text, text[:200])
        expect("empty row dropped from text", "Blank" not in text)
        expect("empty row dropped from html", "Blank" not in html)
        expect("relative CTA became absolute",
               "https://hrms.example.com/leaves" in html
               and "https://hrms.example.com/leaves" in text)
        expect("greeting rendered", "Hi Alice," in text and "Hi Alice," in html)

        # --- 2. a disabled event is not sent --------------------------------
        before = len(inbox)
        sent = await send_event_email(
            "task_assigned", "bob@test.local",
            subject="New task", headline="A task",
        )
        await asyncio.sleep(0.5)
        expect("disabled event returns False", sent is False, f"got {sent}")
        expect("disabled event not delivered", len(inbox) == before)

        # --- 3. no recipient ------------------------------------------------
        expect("empty recipient returns False",
               await send_event_email("leave_decision", "",
                                      subject="x", headline="y") is False)

        # --- 4. attachment path waits and raises when off -------------------
        try:
            await send_email_with_pdf(
                "carol@test.local", "Payslip", "text body",
                b"%PDF-1.4 fake", "payslip.pdf", event="task_complete",
            )
            bad("disabled attachment event raises")
        except RuntimeError as e:
            expect("disabled attachment event raises",
                   "switched off" in str(e), str(e))

        n = len(inbox)
        await send_email_with_pdf(
            "carol@test.local", "Payslip — September 2026", "text body",
            b"%PDF-1.4 fake", "payslip.pdf", event="payslip_ready",
            html="<p>html body</p>",
        )
        expect("payslip delivered synchronously", len(inbox) == n + 1)
        pm = inbox[-1]
        expect("payslip is mixed multipart",
               pm.get_content_type() == "multipart/mixed", pm.get_content_type())
        names = [p.get_filename() for p in pm.walk() if p.get_filename()]
        expect("pdf attached", names == ["payslip.pdf"], str(names))

        # --- 5. delivery log ------------------------------------------------
        rows = [r async for r in db.email_log.find(
            {"to": {"$in": ["alice@test.local", "carol@test.local",
                            "bob@test.local"]}}).sort("createdAt", 1)]
        expect("one log row per delivered mail", len(rows) == 2,
               f"rows={[(r['event'], r['to'], r['status']) for r in rows]}")
        expect("rows marked sent",
               all(r["status"] == "sent" for r in rows),
               str([r["status"] for r in rows]))
        expect("attempts recorded", all(r["attempts"] == 1 for r in rows))
        expect("disabled event left no row",
               not any(r["to"] == "bob@test.local" for r in rows))
        expect("log stores no body",
               all("content" not in r and "body" not in r for r in rows))
        expect("log has expiry for TTL", all(r.get("expiresAt") for r in rows))
        await db.email_log.delete_many(
            {"to": {"$in": ["alice@test.local", "carol@test.local"]}})

        # --- 6. retry then give up when the server is gone ------------------
        ctrl.stop()
        n = len(inbox)
        await send_event_email("leave_decision", "dave@test.local",
                               subject="x", headline="y")
        await asyncio.sleep(6)
        row = await db.email_log.find_one({"to": "dave@test.local"})
        expect("failed send is logged as failed",
               row and row["status"] == "failed", str(row))
        expect("retried up to the cap",
               row and row["attempts"] == 3, str(row and row.get("attempts")))
        expect("failure reason captured", bool(row and row.get("error")),
               str(row and row.get("error")))
        await db.email_log.delete_many({"to": "dave@test.local"})
    finally:
        try: ctrl.stop()
        except Exception: pass

    print(f"\n{P[0]} passed, {F[0]} failed")
    sys.exit(1 if F[0] else 0)

ctrl.start()
asyncio.run(main())
