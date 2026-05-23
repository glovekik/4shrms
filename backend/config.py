import os
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env on import so every process (uvicorn, scripts, scheduler)
# sees the same SMTP / geofence / company config without re-exporting shells.
# override=True so .env wins over leftover shell vars from prior sessions —
# in production, just don't ship a .env file and platform vars take over.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "attendance_secret_key",
)

ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24


# ================= COMPANY (used in payslip PDFs / emails) =================
COMPANY_NAME = os.getenv(
    "COMPANY_NAME",
    "Your Company",
)
COMPANY_ADDRESS = os.getenv(
    "COMPANY_ADDRESS",
    "",
)
# Local path to a logo image (PNG/JPG). Empty = no logo on the payslip.
COMPANY_LOGO_PATH = os.getenv(
    "COMPANY_LOGO_PATH",
    "",
)


# ================= SMTP =================
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
SMTP_USE_TLS = (
    os.getenv("SMTP_USE_TLS", "true").lower() == "true"
)


def is_email_configured() -> bool:
    """Email features fail with a friendly 503 when these aren't set."""
    return bool(SMTP_HOST and SMTP_FROM)


# ================= GEOFENCE =================
def _opt_float(name: str):
    v = os.getenv(name)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


OFFICE_LATITUDE = _opt_float("OFFICE_LATITUDE")
OFFICE_LONGITUDE = _opt_float("OFFICE_LONGITUDE")
OFFICE_RADIUS_METERS = float(
    os.getenv("OFFICE_RADIUS_METERS", "200")
)


def is_geofence_configured() -> bool:
    return (
        OFFICE_LATITUDE is not None
        and OFFICE_LONGITUDE is not None
    )


# ================= ATTENDANCE POLICY =================
# Defaults pulled from PRD sections 5 + 22. Tunable per company via .env.
LATE_AFTER_HOUR = int(os.getenv("LATE_AFTER_HOUR", "10"))
LATE_AFTER_MINUTE = int(os.getenv("LATE_AFTER_MINUTE", "15"))
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "15"))
HALF_DAY_MIN_HOURS = float(os.getenv("HALF_DAY_MIN_HOURS", "4.5"))
OVERTIME_AFTER_HOURS = float(os.getenv("OVERTIME_AFTER_HOURS", "9"))
# Comma-separated weekday numbers (Mon=0, Sun=6). Default Sat+Sun.
WEEKEND_DAYS = [
    int(x) for x in os.getenv("WEEKEND_DAYS", "5,6").split(",") if x.strip()
]


# ================= UPLOADS =================
# Local file storage. For real production, swap to S3/Cloudflare R2 — the
# /uploads endpoint stays the same; only the implementation changes.
UPLOAD_DIR = os.getenv(
    "UPLOAD_DIR",
    str(Path(__file__).resolve().parent / "uploads"),
)
MAX_UPLOAD_BYTES = int(
    os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))  # 20 MB
)
# Public base URL used when constructing returned file URLs.
# Set to your backend's public origin in prod (e.g. https://api.example.com).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")


# ================= PUBLIC CAREERS / OFFER ACCEPT =================
OFFER_ACCEPT_URL_TEMPLATE = os.getenv(
    "OFFER_ACCEPT_URL_TEMPLATE",
    "",
)


# ================= OTP (optional 2FA on login) =================
# When true, /auth/login returns 202 + sends an OTP. Client then calls
# /auth/verify-otp with the code to receive the JWT. When false (default),
# /auth/login behaves as before — single-step, no OTP.
REQUIRE_LOGIN_OTP = (
    os.getenv("REQUIRE_LOGIN_OTP", "false").lower() == "true"
)
OTP_TTL_MINUTES = int(os.getenv("OTP_TTL_MINUTES", "10"))


# ================= INTERN PAYROLL VISIBILITY =================
# When true, employees with tag="Intern" cannot view their own payslips
# or download payslip PDFs. HR still sees everything.
RESTRICT_PAYROLL_FOR_INTERNS = (
    os.getenv("RESTRICT_PAYROLL_FOR_INTERNS", "false").lower() == "true"
)


# ================= PASSWORD RESET =================
# Frontend deep link template; {token} is substituted before emailing.
PASSWORD_RESET_URL_TEMPLATE = os.getenv(
    "PASSWORD_RESET_URL_TEMPLATE",
    "",
)
PASSWORD_RESET_TTL_HOURS = int(
    os.getenv("PASSWORD_RESET_TTL_HOURS", "1")
)
