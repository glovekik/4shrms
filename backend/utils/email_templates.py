"""HTML + plain-text email bodies, built from one structured description.

Every email is described once — a headline, some label/value rows, an
optional call-to-action — and rendered into both formats. Writing the two
by hand guarantees they drift; generating them means the plain-text part is
always an accurate fallback, which also keeps spam scores down.

Why a Python module rather than the templates/email/*.html files the
proposal sketched: HTML email cannot use a stylesheet (Gmail strips <style>
in several contexts), so every rule has to be inlined on the element
anyway. That removes most of the benefit of separate template files while
adding a template directory that has to survive the Docker build. One
module with a shared shell is easier to keep correct.

Deliberately light-only. Dark-mode support across mail clients is
inconsistent enough that a palette designed for both tends to look wrong in
both; a light email renders predictably everywhere.
"""

from html import escape
from typing import Optional, Sequence

from config import APP_BASE_URL, COMPANY_NAME, EMAIL_REPLY_TO

# Kept close to the app's own accent so an email and the screen it links to
# don't look like different products.
_INK = "#16161d"
_INK_2 = "#4a4b5c"
_INK_3 = "#7b7c8d"
_RULE = "#e5e5ee"
_GROUND = "#f5f5f8"
_PANEL = "#ffffff"
_ACCENT = "#4f46e5"

Row = tuple[str, object]


def _rows_html(rows: Sequence[Row]) -> str:
    out = []
    for label, value in rows:
        if value is None or value == "":
            continue
        out.append(
            f'<tr>'
            f'<td style="padding:7px 16px 7px 0;color:{_INK_3};'
            f'font-size:13px;white-space:nowrap;vertical-align:top">'
            f'{escape(str(label))}</td>'
            f'<td style="padding:7px 0;color:{_INK};font-size:14px;'
            f'font-weight:600;vertical-align:top">'
            f'{escape(str(value))}</td>'
            f'</tr>'
        )
    if not out:
        return ""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;margin:18px 0 4px">'
        + "".join(out) +
        '</table>'
    )


def _cta_html(cta: Optional[tuple[str, str]]) -> str:
    if not cta:
        return ""
    label, path = cta
    url = path if path.startswith("http") else f"{APP_BASE_URL}/{path.lstrip('/')}"
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" style="margin:26px 0 6px"><tr><td '
        f'style="background:{_ACCENT};border-radius:6px">'
        f'<a href="{escape(url, quote=True)}" '
        f'style="display:inline-block;padding:11px 22px;color:#ffffff;'
        f'font-size:14px;font-weight:600;text-decoration:none">'
        f'{escape(label)}</a></td></tr></table>'
    )


def render(
    *,
    headline: str,
    greeting: Optional[str] = None,
    intro: Optional[str] = None,
    rows: Optional[Sequence[Row]] = None,
    cta: Optional[tuple[str, str]] = None,
    outro: Optional[str] = None,
    note: Optional[str] = None,
) -> tuple[str, str]:
    """Build (html, text) for one email.

    `rows` are label/value pairs; empty values are dropped rather than
    rendered as a blank line, so an optional field costs the caller nothing.
    `cta` is (label, path-or-url) — a relative path is resolved against
    APP_BASE_URL, because a mail client cannot resolve one itself.
    """
    rows = rows or []

    # ---- HTML ----
    parts = [
        f'<div style="margin:0;padding:24px 12px;background:{_GROUND};'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        f'Roboto,Helvetica,Arial,sans-serif">',
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" style="max-width:560px;margin:0 auto;width:100%">',
        f'<tr><td style="padding:0 0 14px;color:{_INK_3};font-size:12px;'
        f'letter-spacing:.08em;text-transform:uppercase;font-weight:600">'
        f'{escape(COMPANY_NAME)}</td></tr>',
        f'<tr><td style="background:{_PANEL};border:1px solid {_RULE};'
        f'border-radius:10px;padding:28px 26px">',
        f'<h1 style="margin:0 0 14px;font-size:19px;line-height:1.35;'
        f'color:{_INK};font-weight:700">{escape(headline)}</h1>',
    ]
    if greeting:
        parts.append(
            f'<p style="margin:0 0 12px;font-size:14px;color:{_INK_2};'
            f'line-height:1.6">Hi {escape(greeting)},</p>'
        )
    if intro:
        parts.append(
            f'<p style="margin:0 0 4px;font-size:14px;color:{_INK_2};'
            f'line-height:1.6">{escape(intro)}</p>'
        )
    parts.append(_rows_html(rows))
    parts.append(_cta_html(cta))
    if outro:
        parts.append(
            f'<p style="margin:18px 0 0;font-size:14px;color:{_INK_2};'
            f'line-height:1.6">{escape(outro)}</p>'
        )
    if note:
        parts.append(
            f'<p style="margin:18px 0 0;padding:11px 13px;background:{_GROUND};'
            f'border-radius:6px;font-size:13px;color:{_INK_3};'
            f'line-height:1.55">{escape(note)}</p>'
        )
    parts.append('</td></tr>')

    reply_line = (
        f' Replies go to {escape(EMAIL_REPLY_TO)}.' if EMAIL_REPLY_TO else ""
    )
    parts.append(
        f'<tr><td style="padding:16px 4px 0;color:{_INK_3};font-size:12px;'
        f'line-height:1.55">You received this because you have an account '
        f'on {escape(COMPANY_NAME)}’s HR system.{reply_line}</td></tr>'
    )
    parts.append('</table></div>')
    html = "".join(parts)

    # ---- Plain text, from the same data ----
    lines = []
    if greeting:
        lines.append(f"Hi {greeting},")
        lines.append("")
    lines.append(headline)
    lines.append("")
    if intro:
        lines.append(intro)
        lines.append("")
    width = max(
        (len(str(label)) for label, value in rows
         if value is not None and value != ""),
        default=0,
    )
    for label, value in rows:
        if value is None or value == "":
            continue
        lines.append(f"{str(label) + ':':<{width + 2}}{value}")
    if rows:
        lines.append("")
    if cta:
        label, path = cta
        url = (
            path if path.startswith("http")
            else f"{APP_BASE_URL}/{path.lstrip('/')}"
        )
        lines.append(f"{label}: {url}")
        lines.append("")
    if outro:
        lines.append(outro)
        lines.append("")
    if note:
        lines.append(note)
        lines.append("")
    lines.append(f"Regards,\n{COMPANY_NAME}")
    text = "\n".join(lines)

    return html, text
