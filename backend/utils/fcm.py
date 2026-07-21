"""Direct Firebase Cloud Messaging (FCM HTTP v1) sender.

Enabled when a service account is provided via one of:
  * FCM_SERVICE_ACCOUNT_JSON  – the raw service-account JSON (string)
  * FCM_SERVICE_ACCOUNT_FILE  – a path to the service-account JSON file

No-ops (best-effort, never raises) when unconfigured or when google-auth
isn't installed, so the app runs fine without FCM — Expo push still handles
ExponentPushToken[...] tokens (see push.py). google-auth is imported lazily
inside `_load()` so importing this module never fails.
"""

import json
import os
from typing import Optional

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_creds = None
_project: Optional[str] = None
_init_tried = False


def _load():
    """Load service-account credentials once. Returns the creds or None."""
    global _creds, _project, _init_tried
    if _init_tried:
        return _creds
    _init_tried = True

    raw = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.getenv("FCM_SERVICE_ACCOUNT_FILE", "").strip()
    sa = None
    try:
        if raw:
            sa = json.loads(raw)
        elif path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                sa = json.load(f)
    except Exception as e:
        print(f"[fcm] bad service account: {e}")
        return None

    if not sa:
        print("[fcm] no service account configured — FCM disabled (Expo push still active)")
        return None

    try:
        from google.oauth2 import service_account  # lazy
        _creds = service_account.Credentials.from_service_account_info(
            sa, scopes=[_SCOPE]
        )
        _project = os.getenv("FCM_PROJECT_ID", "").strip() or sa.get("project_id")
        print(f"[fcm] configured for project {_project}")
    except Exception as e:
        print(f"[fcm] init failed (is google-auth installed?): {e}")
        return None
    return _creds


def fcm_available() -> bool:
    return _load() is not None


def _access_token() -> Optional[str]:
    creds = _load()
    if not creds:
        return None
    try:
        from google.auth.transport.requests import Request as GoogleRequest  # lazy
        if not creds.valid:
            creds.refresh(GoogleRequest())
        return creds.token
    except Exception as e:
        print(f"[fcm] token refresh failed: {e}")
        return None


async def send_fcm(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    channel_id: Optional[str] = None,
    sound: str = "default",
) -> list[str]:
    """Send a notification to native FCM tokens via HTTP v1.

    Returns the list of tokens FCM reported as invalid/unregistered so the
    caller can prune them. Best-effort — never raises.
    """
    if not tokens or not _load():
        return []
    access = _access_token()
    if not access:
        return []

    import httpx  # already a backend dependency

    url = f"https://fcm.googleapis.com/v1/projects/{_project}/messages:send"
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    # FCM data values must be strings.
    str_data = {k: str(v) for k, v in (data or {}).items()}
    android: dict = {"priority": "high"}
    notif = {}
    if channel_id:
        notif["channel_id"] = channel_id
    if sound:
        notif["sound"] = sound
    if notif:
        android["notification"] = notif

    invalid: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for tok in tokens:
                message = {
                    "message": {
                        "token": tok,
                        "notification": {"title": title, "body": body},
                        "data": str_data,
                        "android": android,
                    }
                }
                try:
                    r = await client.post(url, headers=headers, json=message)
                    if r.status_code >= 400:
                        txt = r.text[:300]
                        print(f"[fcm] {r.status_code}: {txt}")
                        up = txt.upper()
                        if "UNREGISTERED" in up or "NOT_FOUND" in up or "INVALID_ARGUMENT" in up:
                            invalid.append(tok)
                except Exception as e:
                    print(f"[fcm] send failed for one token: {e}")
    except Exception as e:
        print(f"[fcm] send failed: {e}")
    return invalid
