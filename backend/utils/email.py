"""SMTP email delivery: gated, logged, retried, and off the request path.

Three things sit between a caller and the wire.

**A per-event allow-list.** Eleven code paths already call this module. The
moment SMTP credentials are set they would all begin sending at once, so
nothing sends unless its event name appears in EMAIL_ENABLED_EVENTS. Turn
events on one at a time, starting with something low-stakes.

**A delivery log.** `email_log` records every attempt with its outcome.
Without it "did payroll actually go out?" has no answer — the previous
sender swallowed failures into a print statement.

**Retries in the background.** A business action that has already been
committed (an approved leave, a sent payslip) must not be held open, or
rolled back, because an SMTP server is slow. Sends are dispatched as
background tasks and retried with backoff; only the payslip path, where the
caller reports failure to HR directly, waits for the result.
"""

import asyncio
import smtplib

from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional, Sequence

from config import (
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_FROM,
    SMTP_USE_TLS,
    EMAIL_FROM_NAME,
    EMAIL_REPLY_TO,
    EMAIL_MAX_ATTEMPTS,
    EMAIL_RETRY_BACKOFF_SECONDS,
    EMAIL_LOG_TTL_DAYS,
    is_email_configured,
    is_email_event_enabled,
)
from utils.email_templates import Row, render

# asyncio holds only a weak reference to a bare task, so a fire-and-forget
# send can be garbage-collected mid-flight. Keeping a strong reference until
# it finishes is the documented fix.
_inflight: set[asyncio.Task] = set()

# One line per disabled event, not one per send — otherwise a staged
# rollout fills the log with the same message thousands of times.
_warned_disabled: set[str] = set()


# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------
def _build(
    to_email: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    attachment: Optional[tuple[bytes, str]] = None,
):
    """multipart/alternative, with the plain part first as RFC 2046 requires
    — clients render the last part they understand."""
    if attachment:
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain", "utf-8"))
        if html:
            alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)
        blob, filename = attachment
        part = MIMEApplication(blob, _subtype="pdf")
        part.add_header(
            "Content-Disposition", "attachment", filename=filename
        )
        msg.attach(part)
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(text, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = formataddr((EMAIL_FROM_NAME, SMTP_FROM))
    msg["To"] = to_email
    if EMAIL_REPLY_TO:
        msg["Reply-To"] = EMAIL_REPLY_TO
    return msg


def _blocking_send(msg) -> None:
    """One SMTP conversation. Runs in a worker thread — smtplib is blocking
    and would otherwise stall the whole event loop for its 30s timeout."""
    if SMTP_PORT == 465:
        # Implicit TLS: the socket is encrypted from the first byte, so
        # STARTTLS is not just unnecessary but a protocol error here.
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Delivery log
# ---------------------------------------------------------------------------
async def _log_queued(
    event: str, to_email: str, subject: str, meta: Optional[dict]
):
    from database import db
    now = datetime.now(timezone.utc)
    try:
        res = await db.email_log.insert_one({
            "event": event,
            "to": to_email,
            "subject": subject,
            "status": "queued",
            "attempts": 0,
            "error": None,
            "meta": meta or {},
            "createdAt": now,
            "updatedAt": now,
            "sentAt": None,
            "expiresAt": now + timedelta(days=EMAIL_LOG_TTL_DAYS),
        })
        return res.inserted_id
    except Exception as e:
        # A log failure must never stop the email itself.
        print(f"[email] could not write email_log row: {e}")
        return None


async def _log_result(log_id, *, status: str, attempts: int, error=None):
    if log_id is None:
        return
    from database import db
    now = datetime.now(timezone.utc)
    try:
        await db.email_log.update_one(
            {"_id": log_id},
            {"$set": {
                "status": status,
                "attempts": attempts,
                "error": error,
                "updatedAt": now,
                "sentAt": now if status == "sent" else None,
            }},
        )
    except Exception as e:
        print(f"[email] could not update email_log row: {e}")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
async def _attempt_delivery(msg, log_id, *, label: str) -> bool:
    """Send with backoff. Returns True on success; never raises."""
    last_error = ""
    for attempt in range(1, max(1, EMAIL_MAX_ATTEMPTS) + 1):
        try:
            await asyncio.to_thread(_blocking_send, msg)
            await _log_result(log_id, status="sent", attempts=attempt)
            return True
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
        ) as e:
            # The address itself is wrong. Retrying sends the same bad
            # address to the same server for the same answer.
            last_error = f"{type(e).__name__}: {e}"
            await _log_result(
                log_id, status="failed", attempts=attempt, error=last_error
            )
            print(f"[email] {label} rejected: {last_error}")
            return False
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < max(1, EMAIL_MAX_ATTEMPTS):
                await asyncio.sleep(EMAIL_RETRY_BACKOFF_SECONDS * attempt)
    await _log_result(
        log_id,
        status="failed",
        attempts=max(1, EMAIL_MAX_ATTEMPTS),
        error=last_error,
    )
    print(f"[email] {label} failed after retries: {last_error}")
    return False


def _dispatch(coro) -> None:
    """Run a send in the background, keeping a reference so it survives."""
    task = asyncio.create_task(coro)
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def send_event_email(
    event: str,
    to_email: str,
    *,
    subject: str,
    headline: str,
    greeting: Optional[str] = None,
    intro: Optional[str] = None,
    rows: Optional[Sequence[Row]] = None,
    cta: Optional[tuple[str, str]] = None,
    outro: Optional[str] = None,
    note: Optional[str] = None,
    meta: Optional[dict] = None,
    attachment: Optional[tuple[bytes, str]] = None,
    wait: bool = False,
) -> bool:
    """Queue one templated email. Never raises.

    Returns True when the send was accepted for delivery — or, with
    wait=True, when it actually succeeded. False means it was skipped
    (no address, email off, event disabled) or, with wait=True, failed.
    """
    if not to_email or not is_email_configured():
        return False
    if not is_email_event_enabled(event):
        if event not in _warned_disabled:
            _warned_disabled.add(event)
            print(
                f"[email] event '{event}' is not in EMAIL_ENABLED_EVENTS — "
                "not sending. Add it there to switch it on."
            )
        return False

    html, text = render(
        headline=headline,
        greeting=greeting,
        intro=intro,
        rows=rows,
        cta=cta,
        outro=outro,
        note=note,
    )
    msg = _build(to_email, subject, text, html, attachment)
    log_id = await _log_queued(event, to_email, subject, meta)
    label = f"{event} → {to_email}"

    if wait:
        return await _attempt_delivery(msg, log_id, label=label)
    _dispatch(_attempt_delivery(msg, log_id, label=label))
    return True


async def send_event_email_many(
    event: str,
    recipients: Sequence[dict],
    *,
    cta_for=None,
    **kwargs,
) -> int:
    """Same email to several people, each addressed by their own name.

    Recipients are {email, name, role, ...} dicts. Sent individually rather
    than as one message with many To: addresses, so nobody learns who else
    was told.

    `cta_for` receives the recipient dict and returns that person's button,
    for the common case where a manager and an HR user act on the same
    request from different screens.
    """
    sent = 0
    for r in recipients:
        email = r.get("email")
        if not email:
            continue
        per = dict(kwargs)
        if cta_for is not None:
            per["cta"] = cta_for(r)
        if await send_event_email(
            event, email, greeting=r.get("name") or None, **per
        ):
            sent += 1
    return sent


async def send_email_with_pdf(
    to_email: str,
    subject: str,
    body: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    event: str = "payslip_ready",
    html: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Send a PDF and wait for the outcome.

    Unlike every other path this one raises, because its caller (payroll)
    reports the result to HR in the response — "payslip emailed" has to be
    true when it says so.
    """
    if not is_email_configured():
        raise RuntimeError("Email is not configured (SMTP_HOST / SMTP_FROM)")
    if not is_email_event_enabled(event):
        raise RuntimeError(
            f"Email event '{event}' is switched off. Add it to "
            "EMAIL_ENABLED_EVENTS to send."
        )

    msg = _build(
        to_email, subject, body, html, attachment=(pdf_bytes, pdf_filename)
    )
    log_id = await _log_queued(event, to_email, subject, meta)
    ok = await _attempt_delivery(
        msg, log_id, label=f"{event} → {to_email}"
    )
    if not ok:
        raise RuntimeError(
            "SMTP delivery failed — see the email log for the reason."
        )
