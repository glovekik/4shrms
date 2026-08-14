"""Payslip PDF generation via reportlab.

Self-contained — feed in payslip + user dicts, get bytes back.
"""

import os
from io import BytesIO
from calendar import month_name, monthrange

from utils.ist import now_ist_naive

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import (
    getSampleStyleSheet,
    ParagraphStyle,
)
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
)

from config import (
    COMPANY_NAME,
    COMPANY_ADDRESS,
    COMPANY_LOGO_PATH,
)


def _money(v) -> str:
    try:
        return f"INR {float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "INR 0.00"


def _num(v) -> str:
    """Whole-rupee amount, no currency symbol — matches the reference payslip.

    Thousands-separated to match the approved payslip ("10,000", not
    "10000").

    MONEY ONLY. Do not use for day counts: rounding to int turns a 1.5-day
    LOP into 2 and a 0.5-day into 0, and a day count should not carry a
    thousands separator. Use _days() for those.
    """
    try:
        return f"{int(round(float(v or 0))):,}"
    except (TypeError, ValueError):
        return "0"


def _days(v) -> str:
    """Day count, keeping the half. 22 -> "22", 1.5 -> "1.5", 0.5 -> "0.5"."""
    try:
        f = round(float(v or 0), 2)
    except (TypeError, ValueError):
        return "0"
    return str(int(f)) if f == int(f) else f"{f:g}"


def _rupees_in_words(amount) -> str:
    """Indian-system rupees in words, e.g. 15452 -> 'Rupees fifteen thousand
    four hundred fifty two Only'. No external dependency."""
    try:
        n = int(round(float(amount or 0)))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "Rupees Zero Only"

    ones = [
        "", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
        "sixteen", "seventeen", "eighteen", "nineteen",
    ]
    tens = [
        "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
        "eighty", "ninety",
    ]

    def two(x: int) -> str:
        # Compound tens are hyphenated ("ninety-two"), matching the approved
        # payslip wording.
        if x < 20:
            return ones[x]
        return (tens[x // 10] + ("-" + ones[x % 10] if x % 10 else "")).strip()

    def three(x: int) -> str:
        h, r = x // 100, x % 100
        out = f"{ones[h]} hundred" if h else ""
        if r:
            out += (" " if out else "") + two(r)
        return out

    crore = n // 10000000
    lakh = (n // 100000) % 100
    thousand = (n // 1000) % 100
    hundreds = n % 1000

    parts = []
    if crore:
        parts.append(two(crore) + " crore")
    if lakh:
        parts.append(two(lakh) + " lakh")
    if thousand:
        parts.append(two(thousand) + " thousand")
    if hundreds:
        parts.append(three(hundreds))

    return "Rupees " + " ".join(parts).strip() + " Only"


def _fmt_date(v) -> str:
    """Render a stored date (YYYY-MM-DD or ISO) as e.g. '11 Apr 2022'."""
    if not v:
        return "—"
    s = str(v)
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return _dt.strptime(s[: len(fmt) + 6], fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return s[:10]


def _identity(payslip: dict, user: dict) -> dict:
    """Resolve every identity field the payslip shows, preferring the value
    snapshotted on the payslip, then falling back to the employee's profile.

    This is the fix for "details entered aren't reflected": the PF number, PAN,
    UAN, bank and ESI live on the user's statutory / bank profile, but the
    payslip snapshot (taken from the salary structure) didn't carry all of
    them. Reading through to the user here means whatever HR entered shows up.
    """
    stat = (user.get("statutory") or {}) if isinstance(user, dict) else {}
    work = (user.get("work") or {}) if isinstance(user, dict) else {}
    banks = user.get("bankAccounts") or []
    bank0 = banks[0] if isinstance(banks, list) and banks else {}

    def pick(*vals):
        for v in vals:
            if v not in (None, "", "—"):
                return v
        return "—"

    # The employee PROFILE is the source of truth for identity, so an HR edit to
    # the PF number / PAN / bank shows up on the payslip. The payslip's own
    # snapshot (copied from the salary structure at generation) is only the
    # fallback for fields the profile doesn't carry.
    return {
        "name": pick(user.get("name")),
        "employeeCode": pick(user.get("employeeCode")),
        "joiningDate": _fmt_date(user.get("joiningDate")),
        "designation": pick(
            work.get("jobTitle"), work.get("jobPosition"), user.get("tag")
        ),
        "department": pick(user.get("departmentName")),
        "location": pick(work.get("workLocation"), user.get("location")),
        "pan": pick(stat.get("pan"), payslip.get("panNumber")),
        "uan": pick(stat.get("uan"), payslip.get("uanNumber")),
        "pfNumber": pick(
            stat.get("pfAccountNumber"), payslip.get("pfNumber")
        ),
        "esiNumber": pick(stat.get("esiNumber"), payslip.get("esiNumber")),
        "bankName": pick(
            bank0.get("bankName"), payslip.get("bankName")
        ),
        "bankAccountNumber": pick(
            bank0.get("accountNumber"),
            bank0.get("number"),
            payslip.get("bankAccountNumber"),
        ),
    }


def _styles():
    base = getSampleStyleSheet()
    title = ParagraphStyle(
        "Title",
        parent=base["Title"],
        fontSize=16,
        spaceAfter=4,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=base["Heading2"],
        fontSize=12,
        spaceBefore=4,
        spaceAfter=6,
    )
    small = ParagraphStyle(
        "Small",
        parent=base["Normal"],
        fontSize=8,
        textColor=colors.grey,
    )
    footer = ParagraphStyle(
        "Footer",
        parent=base["Italic"],
        fontSize=8,
        textColor=colors.grey,
        alignment=1,  # center
    )
    return base, title, h2, small, footer


# Bump this whenever the payslip LAYOUT changes, so cached PDFs generated by an
# older layout are transparently regenerated on next download instead of served
# stale. (See routes/payroll.py download cache.)
# Bump this whenever the payslip LAYOUT changes, so cached PDFs generated by an
# older layout are transparently regenerated on next download instead of served
# stale. (See routes/payroll.py download cache.)
PAYSLIP_PDF_VERSION = 7

# Neutral palette — the layout carries the structure, not colour. Section
# bars and totals are distinguished by a light grey fill and bold type, so
# the slip prints cleanly in mono and reads as a formal document.
BAR_BG = colors.HexColor("#E4E8EE")   # section bars (period, earnings, etc.)
PALE = colors.HexColor("#F0F2F6")     # table headers, totals, label cells
INK = colors.HexColor("#1A1A1A")      # bar + heading text


def _bar(text: str, style, bg, width: float = 182 * mm) -> Table:
    """A full-width coloured section bar with white bold text."""
    t = Table([[Paragraph(text, style)]], colWidths=[width])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#B9C0CA")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ])
    )
    return t


def build_payslip_pdf(
    payslip: dict,
    user: dict,
) -> bytes:
    """PDF bytes for a payslip, in the standard Indian salary-slip layout.

    Structure (matches the approved reference slip):
      company header → period bar → identity grid →
      Earnings (Full / Actual) beside Deductions → Net Pay (A - B) →
      Employer's Contribution (C) → Total CTC (A + C).

    IMPORTANT — employer PF and employer-paid insurance are NOT earnings.
    They are cost to company, not money the employee receives, so they sit in
    their own "Employer's Contribution (Not part of Net Pay)" block and are
    excluded from Gross (A) and therefore from Net Pay. An earlier version
    counted them as earnings, which made the payslip's net materially higher
    than the actual bank credit.
    """

    base, TitleStyle, H2Style, SmallStyle, FooterStyle = _styles()

    ident = _identity(payslip, user)
    month = payslip.get("month", 1)
    year = payslip.get("year", "")
    m_idx = month if month in range(1, 13) else 1
    period = f"{month_name[m_idx]} {year}"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"Payslip — {ident['name']} — {period}",
    )

    hairline = colors.HexColor("#B9C0CA")
    grey = colors.HexColor("#333333")

    company_style = ParagraphStyle(
        "Co", parent=base["Normal"], fontSize=13, leading=16,
        alignment=1, fontName="Helvetica-Bold",
    )
    addr_style = ParagraphStyle(
        "Addr", parent=base["Normal"], fontSize=8.5, leading=11,
        alignment=1, textColor=grey,
    )
    bar_style = ParagraphStyle(
        "Bar", parent=base["Normal"], fontSize=10.5, leading=13,
        alignment=1, fontName="Helvetica-Bold",
        textColor=INK,
    )
    bar_left = ParagraphStyle(
        "BarL", parent=bar_style, alignment=0,
    )
    words_style = ParagraphStyle(
        "Words", parent=base["Italic"], fontSize=9, leading=12,
    )
    note_style = ParagraphStyle(
        "Note", parent=base["Italic"], fontSize=7.5, leading=10,
        textColor=grey,
    )

    elements = []

    # ----- Logo + company header -----
    if COMPANY_LOGO_PATH and os.path.isfile(COMPANY_LOGO_PATH):
        try:
            logo = Image(COMPANY_LOGO_PATH, width=46 * mm, height=18.3 * mm)
            logo.hAlign = "CENTER"
            elements.append(logo)
            elements.append(Spacer(1, 1.5 * mm))
        except Exception:
            pass

    elements.append(Paragraph(COMPANY_NAME, company_style))
    if COMPANY_ADDRESS:
        elements.append(Paragraph(COMPANY_ADDRESS, addr_style))
    elements.append(Spacer(1, 3 * mm))

    # ----- Period bar -----
    elements.append(
        _bar(f"Payslip for the month of {period}", bar_style, BAR_BG)
    )
    elements.append(Spacer(1, 2 * mm))

    # ----- Identity grid -----
    # Date of issue = last day of the payslip month, which is when payroll
    # for that month is finalised.
    last_day = monthrange(int(year), m_idx)[1] if str(year).isdigit() else 30
    issue_date = _fmt_date(f"{year}-{m_idx:02d}-{last_day:02d}")

    id_rows = [
        ["Name:", ident["name"], "Employee No:", ident["employeeCode"]],
        ["Joining Date:", ident["joiningDate"],
         "Bank Name:", ident["bankName"]],
        ["Designation:", ident["designation"],
         "Bank Account No:", ident["bankAccountNumber"]],
        ["Department:", ident["department"], "PAN Number:", ident["pan"]],
        ["Location:", ident["location"], "PF No:", ident["pfNumber"]],
        ["Effective Work Days:", _days(payslip.get("workingDays")),
         "PF UAN:", ident["uan"]],
        ["LOP:", _days(payslip.get("lopDays")),
         "Date of Issue:", issue_date],
    ]
    # Label columns sized for the longest label ("Effective Work Days:"),
    # which overflowed a 30mm cell and collided with its value.
    id_tbl = Table(id_rows, colWidths=[35 * mm, 56 * mm, 33 * mm, 58 * mm])
    id_tbl.setStyle(
        TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
            ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 8.5),
            ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 8.5),
            ("BACKGROUND", (0, 0), (0, -1), PALE),
            ("BACKGROUND", (2, 0), (2, -1), PALE),
            ("GRID", (0, 0), (-1, -1), 0.4, hairline),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    elements.append(id_tbl)
    elements.append(Spacer(1, 3 * mm))

    # ----- Amounts -----
    wd = float(payslip.get("workingDays") or 0)
    lop = float(payslip.get("lopDays") or 0)
    factor = ((wd - lop) / wd) if wd > 0 else 1.0
    factor = max(0.0, min(1.0, factor))

    def amt(k) -> float:
        try:
            return float(payslip.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    # (A) Cash components only — what the employee is actually paid.
    earn_defs = [
        ("BASIC", "basic"),
        ("HRA", "hra"),
        ("Communication Allowance", "communicationAllowance"),
        ("Other Allowance", "otherAllowance"),
    ]
    earn_lines = [
        (lbl, amt(k), amt(k) * factor) for lbl, k in earn_defs if amt(k) > 0
    ]
    gross_full = sum(f for _, f, _ in earn_lines)
    gross_actual = sum(a for _, _, a in earn_lines)

    # (B) Employee-borne deductions.
    ded_defs = [
        ("PF (Employee)", "employeePF"),
        ("ESI (Employee)", "employeeInsurance"),
        ("Professional Tax", "professionalTax"),
        ("TDS", "tds"),
    ]
    # TDS is shown even at zero — its absence on a payslip reads as an
    # omission rather than "nothing was withheld".
    ded_lines = [
        (lbl, amt(k)) for lbl, k in ded_defs
        if amt(k) > 0 or k == "tds"
    ]
    tot_ded = sum(v for _, v in ded_lines)

    net = round(gross_actual - tot_ded)

    # (C) Employer cost — shown, but never part of net pay.
    emp_defs = [
        ("Employer PF Contribution", "employerPF"),
        ("Health Insurance (Employer-paid)", "employerInsurance"),
    ]
    emp_lines = [(lbl, amt(k)) for lbl, k in emp_defs if amt(k) > 0]
    tot_emp = sum(v for _, v in emp_lines)

    # Keep both columns the same height, with a couple of blank rows so the
    # block doesn't look cramped when there are few deductions.
    n = max(len(earn_lines), len(ded_lines)) + 2

    e_body = [["Component", "Full", "Actual"]]
    for i in range(n):
        if i < len(earn_lines):
            lbl, f, a = earn_lines[i]
            e_body.append([lbl, _num(f), _num(a)])
        else:
            e_body.append(["", "", ""])
    e_body.append(["Gross Salary (A)", _num(gross_full), _num(gross_actual)])

    d_body = [["Component", "Actual"]]
    for i in range(n):
        if i < len(ded_lines):
            lbl, v = ded_lines[i]
            d_body.append([lbl, _num(v)])
        else:
            d_body.append(["", ""])
    d_body.append(["Total Deductions (B)", _num(tot_ded)])

    def _pay_style():
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 8.7),
            ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9),
            ("BACKGROUND", (0, -1), (-1, -1), PALE),
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, hairline),
            ("GRID", (0, 0), (-1, -1), 0.4, hairline),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ])

    # Navy header spanning both columns, like the reference.
    head = Table(
        [[Paragraph("Earnings (Cash Components)", bar_left),
          Paragraph("Deductions", bar_left)]],
        colWidths=[91 * mm, 91 * mm],
    )
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAR_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, hairline),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(head)

    earn_tbl = Table(e_body, colWidths=[51 * mm, 20 * mm, 20 * mm])
    earn_tbl.setStyle(_pay_style())
    ded_tbl = Table(d_body, colWidths=[66 * mm, 25 * mm])
    ded_tbl.setStyle(_pay_style())

    side = Table([[earn_tbl, ded_tbl]], colWidths=[91 * mm, 91 * mm])
    side.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(side)
    elements.append(Spacer(1, 3 * mm))

    # ----- Net pay banner -----
    net_tbl = Table(
        [[
            Paragraph("Net Pay for the month (A - B):", bar_left),
            Paragraph(f"INR {int(net):,}", bar_style),
        ]],
        colWidths=[130 * mm, 52 * mm],
    )
    net_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAR_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, hairline),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(net_tbl)
    elements.append(Spacer(1, 1.5 * mm))
    elements.append(Paragraph(f"({_rupees_in_words(net)})", words_style))
    elements.append(Spacer(1, 3 * mm))

    # ----- Employer's contribution -----
    if emp_lines:
        elements.append(
            _bar("Employer's Contribution (Not part of Net Pay)",
                 bar_left, BAR_BG)
        )
        c_body = [["Component", "Amount (INR)"]]
        for lbl, v in emp_lines:
            c_body.append([lbl, _num(v)])
        c_body.append(["Total Employer Contribution (C)", _num(tot_emp)])
        c_tbl = Table(c_body, colWidths=[121 * mm, 61 * mm])
        c_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("BACKGROUND", (0, -1), (-1, -1), PALE),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
            ("FONT", (0, 1), (-1, -2), "Helvetica", 8.7),
            ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9),
            ("GRID", (0, 0), (-1, -1), 0.4, hairline),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(c_tbl)
        elements.append(Spacer(1, 2.5 * mm))

        ctc_tbl = Table(
            [["Total CTC for the month (A + C):",
              f"INR {int(round(gross_actual + tot_emp)):,}"]],
            colWidths=[121 * mm, 61 * mm],
        )
        ctc_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PALE),
            ("FONT", (0, 0), (0, 0), "Helvetica-Bold", 9.5),
            ("FONT", (1, 0), (1, 0), "Helvetica-Bold", 10.5),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("BOX", (0, 0), (-1, -1), 0.4, hairline),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(ctc_tbl)

    elements.append(Spacer(1, 5 * mm))
    fy_start = year if m_idx >= 4 else (int(year) - 1 if str(year).isdigit() else year)
    fy_end = (int(fy_start) + 1) if str(fy_start).isdigit() else ""
    elements.append(
        Paragraph(
            f"Year-to-Date (FY {fy_start}-{str(fy_end)[-2:]}): Gross Earnings, "
            "Deductions and TDS figures can be appended here once the payroll "
            "register for prior months of the financial year is available.",
            note_style,
        )
    )

    elements.append(Spacer(1, 5 * mm))
    elements.append(
        Paragraph(
            "This is a system generated payslip and does not require "
            "signature.",
            FooterStyle,
        )
    )
    if payslip.get("notes"):
        elements.append(Spacer(1, 3 * mm))
        elements.append(Paragraph(f"Notes: {payslip['notes']}", SmallStyle))

    doc.build(elements)
    return buf.getvalue()


def build_experience_letter_pdf(
    user: dict,
    joining_date: str,
    last_working_day: str,
    designation: str = "Employee",
) -> bytes:
    """Standard experience/relieving letter PDF."""
    base, TitleStyle, H2Style, SmallStyle, FooterStyle = _styles()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f"Experience Letter — {user.get('name', '')}",
    )

    elements = []

    # Company header
    if COMPANY_LOGO_PATH and os.path.isfile(COMPANY_LOGO_PATH):
        try:
            elements.append(
                Image(
                    COMPANY_LOGO_PATH,
                    # Preserve the logo's ~2.57:1 aspect ratio (no squish).
                    width=44 * mm,
                    height=17.1 * mm,
                )
            )
        except Exception:
            pass

    elements.append(
        Paragraph(f"<b>{COMPANY_NAME}</b>", TitleStyle)
    )
    if COMPANY_ADDRESS:
        elements.append(
            Paragraph(COMPANY_ADDRESS, base["Normal"])
        )
    elements.append(Spacer(1, 8 * mm))

    today_str = now_ist_naive().strftime("%B %d, %Y")
    elements.append(
        Paragraph(f"Date: {today_str}", base["Normal"])
    )
    elements.append(Spacer(1, 6 * mm))

    elements.append(
        Paragraph(
            "<b>TO WHOM IT MAY CONCERN</b>",
            H2Style,
        )
    )
    elements.append(Spacer(1, 3 * mm))

    name = user.get("name") or "the employee"
    emp_code = user.get("employeeCode") or "—"

    body_text = (
        f"This is to certify that <b>{name}</b> "
        f"(Employee Code: {emp_code}) was associated with "
        f"<b>{COMPANY_NAME}</b> as <b>{designation}</b> from "
        f"<b>{joining_date}</b> to <b>{last_working_day}</b>."
        "<br/><br/>"
        "During the period of association, the employee was found to be "
        "sincere, hardworking, and professional in conduct. "
        "We wish them the very best in all future endeavours."
        "<br/><br/>"
        "This letter is issued upon request and on the basis of records "
        "available with us at the time of issuance."
    )
    elements.append(Paragraph(body_text, base["Normal"]))
    elements.append(Spacer(1, 15 * mm))

    elements.append(
        Paragraph("Sincerely,", base["Normal"])
    )
    elements.append(Spacer(1, 12 * mm))
    elements.append(
        Paragraph("________________________", base["Normal"])
    )
    elements.append(
        Paragraph(
            f"Authorised Signatory<br/>{COMPANY_NAME}",
            base["Normal"],
        )
    )

    doc.build(elements)
    return buf.getvalue()
