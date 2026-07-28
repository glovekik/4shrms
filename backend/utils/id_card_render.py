"""Server-side ID-card rendering (Pillow).

Composes the employee badge — framed logo, photo, name, role, details, and the
back-of-card info — into a single high-resolution image, then emits it as JPG
or PDF.

Rendering on the server (instead of html2canvas in the browser) means the photo
and logo are ALWAYS embedded, the output is tightly cropped to the two cards
(no giant white screenshot), and it looks identical on every device.
"""
from __future__ import annotations

import os
from io import BytesIO
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

import reportlab

from config import COMPANY_NAME, COMPANY_LOGO_PATH
from utils.face_frame import read_upload_bytes

# reportlab bundles Vera — a clean sans that Pillow can load, so we don't depend
# on system fonts (the slim Docker image has none).
_RL_FONTS = os.path.join(os.path.dirname(reportlab.__file__), "fonts")


def _font(size: int, bold: bool = False, italic: bool = False):
    name = "Vera.ttf"
    if bold:
        name = "VeraBd.ttf"
    elif italic:
        name = "VeraIt.ttf"
    return ImageFont.truetype(os.path.join(_RL_FONTS, name), size)


# ---- geometry (mirrors components/IDCard.tsx, scaled up for print) ----
S = 4                                   # 4x the on-screen 320px card
CARD_W, CARD_H = 320 * S, 507 * S       # 1280 x 2028 per side
GAP = 26 * S                            # space between the two cards
PAD = 26 * S                            # outer white margin

NAVY = (16, 48, 95)
INK = (61, 70, 88)
MUTED = (154, 163, 174)
BORDER = (220, 227, 237)
HAIR = (229, 232, 237)
WHITE = (255, 255, 255)
EM_BG = (253, 243, 243)
EM_RED = (192, 57, 43)

ADDRESS_LINES = [
    "1-1-565/307, Golconda X Road, Bakaram,",
    "Musheerabad (ND), Hyderabad - 500020, Telangana",
]


def _round_mask(size, radius) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255
    )
    return m


def _text_w(draw, text, font) -> float:
    return draw.textlength(text, font=font)


def _fit(draw, text, font_size_bold, max_w, bold=True):
    """Shrink a font until `text` fits `max_w`. Returns the font."""
    size = font_size_bold
    while size > 8:
        f = _font(size, bold=bold)
        if _text_w(draw, text, f) <= max_w:
            return f
        size -= 2
    return _font(size, bold=bold)


def _framed_photo(photo_bytes: Optional[bytes], box: int, framing) -> Image.Image:
    """Cover-fit the photo into a square `box`px, applying the saved zoom/offset,
    with rounded corners."""
    placeholder = Image.new("RGB", (box, box), (228, 232, 236))
    if not photo_bytes:
        img = placeholder
    else:
        try:
            img = Image.open(BytesIO(photo_bytes)).convert("RGB")
        except Exception:
            img = placeholder

    f = framing or {}
    zoom = float(f.get("zoom", 1) or 1)
    ox = float(f.get("offsetX", 0) or 0) * S
    oy = float(f.get("offsetY", 0) or 0) * S

    sw, sh = img.size
    scale = max(box / sw, box / sh) * max(1.0, zoom)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("RGB", (box, box), (228, 232, 236))
    x = (box - nw) // 2 + int(ox)
    y = (box - nh) // 2 + int(oy)
    canvas.paste(resized, (x, y))
    canvas.putalpha(_round_mask((box, box), 20 * S))
    return canvas


def _logo_box(draw: ImageDraw.ImageDraw, card: Image.Image, top: int) -> int:
    """Draw the framed logo at the top; return its bottom y."""
    bw, bh = 206 * S, 72 * S
    bx = (CARD_W - bw) // 2
    draw.rounded_rectangle(
        [bx, top, bx + bw, top + bh], radius=12 * S, fill=WHITE, outline=BORDER,
        width=max(1, S),
    )
    if COMPANY_LOGO_PATH and os.path.isfile(COMPANY_LOGO_PATH):
        try:
            logo = Image.open(COMPANY_LOGO_PATH).convert("RGBA")
            lw = 190 * S
            lh = int(logo.height * (lw / logo.width))
            max_h = bh - 12 * S
            if lh > max_h:
                lh = max_h
                lw = int(logo.width * (lh / logo.height))
            logo = logo.resize((lw, lh), Image.LANCZOS)
            card.paste(
                logo, (bx + (bw - lw) // 2, top + (bh - lh) // 2), logo
            )
        except Exception:
            pass
    return top + bh


def _pretty_date(v) -> str:
    if not v:
        return "-"
    s = str(v)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s[: len(fmt) + 6], fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return s[:10]


def _front(info: dict) -> Image.Image:
    card = Image.new("RGB", (CARD_W, CARD_H), WHITE)
    d = ImageDraw.Draw(card)
    d.rounded_rectangle(
        [0, 0, CARD_W - 1, CARD_H - 1], radius=19 * S, outline=HAIR,
        width=max(1, S),
    )

    y = _logo_box(d, card, 16 * S)

    # photo
    box = 190 * S
    px = (CARD_W - box) // 2
    py = y + 12 * S
    photo_img = _framed_photo(info.get("photo_bytes"), box, info.get("framing"))
    card.paste(photo_img, (px, py), photo_img)
    y = py + box

    # name
    name = info.get("name") or "-"
    nf = _fit(d, name, 25 * S, CARD_W - 40 * S, bold=True)
    ny = y + 14 * S
    d.text(((CARD_W - _text_w(d, name, nf)) / 2, ny), name, font=nf, fill=NAVY)
    y = ny + nf.size

    # role pill
    role = (info.get("designation") or "Employee").upper()
    pf = _fit(d, role, 12 * S, CARD_W - 80 * S, bold=True)
    pw = _text_w(d, role, pf)
    ph = pf.size + 14 * S
    pill_w = pw + 30 * S
    plx = (CARD_W - pill_w) // 2
    ply = y + 12 * S
    d.rounded_rectangle([plx, ply, plx + pill_w, ply + ph], radius=8 * S,
                        fill=NAVY)
    d.text((plx + (pill_w - pw) / 2, ply + (ph - pf.size) / 2 - 2 * S), role,
           font=pf, fill=WHITE)

    # navy blob bottom-right (radius clamped to the blob's height)
    bw, bh = 170 * S, 42 * S
    d.rounded_rectangle(
        [CARD_W - bw, CARD_H - bh, CARD_W, CARD_H], radius=bh // 2, fill=NAVY,
        corners=(True, False, False, False),
    )

    # details (ID No / Email / Phone) — left aligned, above the blob
    rows = [
        ("ID No", info.get("employeeCode") or "-"),
        ("Email", info.get("email") or "-"),
        ("Phone", info.get("phone") or "-"),
    ]
    lf = _font(12 * S, bold=True)
    vf = _font(12 * S)
    row_h = 24 * S
    dx = 36 * S
    start = CARD_H - 50 * S - row_h * len(rows)
    label_w = 52 * S
    for i, (label, value) in enumerate(rows):
        ry = start + i * row_h
        d.text((dx, ry), label, font=lf, fill=NAVY)
        d.text((dx + label_w, ry), ":", font=lf, fill=NAVY)
        val = value
        while val and _text_w(d, val, vf) > CARD_W - dx - label_w - 30 * S:
            val = val[:-1]
        if val != value:
            val = val[:-1] + "…"
        d.text((dx + label_w + 12 * S, ry), val, font=vf, fill=INK)

    return card


def _back(info: dict) -> Image.Image:
    card = Image.new("RGB", (CARD_W, CARD_H), WHITE)
    d = ImageDraw.Draw(card)
    d.rounded_rectangle(
        [0, 0, CARD_W - 1, CARD_H - 1], radius=19 * S, outline=HAIR,
        width=max(1, S),
    )

    y = _logo_box(d, card, 20 * S)

    mx = 24 * S
    d.text((mx, y + 18 * S), "Cardholder details", font=_font(15 * S, bold=True),
           fill=NAVY)
    y = y + 18 * S + 15 * S + 16 * S

    rows = [
        ("Employee ID", info.get("employeeCode") or "-"),
        ("Department", info.get("department") or "-"),
        ("Date of joining", _pretty_date(info.get("joiningDate"))),
        ("Blood group", info.get("bloodGroup") or "-"),
    ]
    lf = _font(11 * S, bold=True)
    vf = _font(12 * S)
    for label, value in rows:
        d.text((mx, y), label, font=lf, fill=NAVY)
        vw = _text_w(d, value, vf)
        d.text((CARD_W - mx - vw, y), value, font=vf, fill=INK)
        d.line([mx, y + 22 * S, CARD_W - mx, y + 22 * S], fill=HAIR,
               width=max(1, S))
        y += 30 * S

    # emergency
    ey = y + 8 * S
    eh = 78 * S
    d.rounded_rectangle([mx, ey, CARD_W - mx, ey + eh], radius=12 * S, fill=EM_BG)
    d.text((mx + 12 * S, ey + 9 * S), "IN CASE OF EMERGENCY",
           font=_font(9 * S, bold=True), fill=EM_RED)
    ec = info.get("emergency") or {}
    if ec.get("contactName"):
        who = ec["contactName"]
        if ec.get("relationship"):
            who += f"  ·  {ec['relationship']}"
        d.text((mx + 12 * S, ey + 30 * S), who, font=_font(11 * S, bold=True),
               fill=NAVY)
        if ec.get("phone"):
            d.text((mx + 12 * S, ey + 52 * S), str(ec["phone"]),
                   font=_font(11 * S, bold=True), fill=EM_RED)
    else:
        d.text((mx + 12 * S, ey + 36 * S), "Not provided",
               font=_font(11 * S, italic=True), fill=MUTED)

    # return-to address + terms, anchored to the bottom (clear of the box above)
    ay = CARD_H - 148 * S
    d.text((mx, ay), "IF FOUND, RETURN TO", font=_font(8 * S, bold=True),
           fill=NAVY)
    d.text((mx, ay + 16 * S), f"{COMPANY_NAME}", font=_font(9 * S, bold=True),
           fill=INK)
    for i, line in enumerate(ADDRESS_LINES):
        d.text((mx, ay + 32 * S + i * 14 * S), line, font=_font(8 * S),
               fill=INK)

    # terms — word-wrapped to the card width so it never runs off the edge
    terms = (f"Property of {COMPANY_NAME}. Must be surrendered on request. "
             "Valid while employed.")
    tf = _font(7 * S)
    max_w = CARD_W - 2 * mx
    ty = ay + 74 * S
    line = ""
    for word in terms.split():
        test = (line + " " + word).strip()
        if _text_w(d, test, tf) <= max_w:
            line = test
        else:
            d.text((mx, ty), line, font=tf, fill=MUTED)
            ty += 11 * S
            line = word
    if line:
        d.text((mx, ty), line, font=tf, fill=MUTED)

    # small navy blob (radius clamped to the blob's height)
    bw, bh = 98 * S, 38 * S
    d.rounded_rectangle(
        [CARD_W - bw, CARD_H - bh, CARD_W, CARD_H], radius=bh // 2, fill=NAVY,
        corners=(True, False, False, False),
    )
    return card


def _compose(info: dict) -> Image.Image:
    front, back = _front(info), _back(info)
    W = PAD * 2 + CARD_W * 2 + GAP
    H = PAD * 2 + CARD_H
    sheet = Image.new("RGB", (W, H), WHITE)
    sheet.paste(front, (PAD, PAD))
    sheet.paste(back, (PAD + CARD_W + GAP, PAD))
    return sheet


def build_info(user: dict, card: dict, department_name: Optional[str]) -> dict:
    """Flatten the fields the badge needs from a user + card doc."""
    stat = user.get("statutory") or {}
    work = user.get("work") or {}
    personal = user.get("personal") or {}
    # Prefer the APPROVED (issued) photo — a pending re-upload shouldn't change
    # the card someone downloads until HR approves it.
    photo_url = card.get("approvedPhotoUrl") or card.get("photoUrl")
    photo_bytes = read_upload_bytes(photo_url) if photo_url else None
    return {
        "name": user.get("name"),
        "employeeCode": user.get("employeeCode"),
        "email": user.get("email"),
        "phone": user.get("workPhone") or personal.get("phone"),
        "designation": work.get("jobTitle") or work.get("jobPosition")
        or user.get("tag"),
        "department": department_name,
        "joiningDate": user.get("joiningDate"),
        "bloodGroup": personal.get("bloodGroup"),
        "emergency": user.get("emergencyContact") or {},
        "photo_bytes": photo_bytes,
        "framing": card.get("framing"),
    }


def render_id_card(info: dict, fmt: str = "pdf") -> bytes:
    """Render the badge (front + back) as JPG or PDF bytes."""
    sheet = _compose(info)
    buf = BytesIO()
    if fmt == "jpg":
        sheet.save(buf, "JPEG", quality=92, dpi=(300, 300))
    else:
        sheet.save(buf, "PDF", resolution=300.0)
    return buf.getvalue()
