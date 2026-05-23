"""SMTP email sender. Blocking smtplib runs in a thread."""

import asyncio
import smtplib

from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_FROM,
    SMTP_USE_TLS,
    is_email_configured,
)


async def send_email_with_pdf(
    to_email: str,
    subject: str,
    body: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> None:
    """Sends a plain-text email with a PDF attachment.

    Raises RuntimeError if SMTP fails — caller turns it into a 502/503.
    """

    def _blocking() -> None:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain"))

        attach = MIMEApplication(pdf_bytes, _subtype="pdf")
        attach.add_header(
            "Content-Disposition",
            "attachment",
            filename=pdf_filename,
        )
        msg.attach(attach)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

    await asyncio.to_thread(_blocking)


async def send_notification_email(
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    """Sends a plain-text notification email. Never raises.

    Why: callers wire this next to push_to_user inside business flows
    (leave decisions, task assignment, etc.). A flaky SMTP must not roll
    back an approval that has already been committed to the DB.

    Returns True on success, False on any failure (logged to stdout).
    """
    if not is_email_configured() or not to_email:
        return False

    def _blocking() -> None:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(_blocking)
        return True
    except Exception as e:
        print(f"[email] notification to {to_email} failed: {e}")
        return False
