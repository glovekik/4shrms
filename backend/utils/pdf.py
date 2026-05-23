"""Payslip PDF generation via reportlab.

Self-contained — feed in payslip + user dicts, get bytes back.
"""

import os
from datetime import datetime
from io import BytesIO
from calendar import month_name

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


def build_payslip_pdf(
    payslip: dict,
    user: dict,
) -> bytes:
    """Returns PDF bytes for the given payslip + user."""

    base, TitleStyle, H2Style, SmallStyle, FooterStyle = _styles()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=(
            f"Payslip — {user.get('name', '')} — "
            f"{month_name[payslip.get('month', 1)]} "
            f"{payslip.get('year', '')}"
        ),
    )

    elements = []

    # ----- Header: logo + company name + period -----
    header_left = []
    if COMPANY_LOGO_PATH and os.path.isfile(COMPANY_LOGO_PATH):
        try:
            header_left.append(
                Image(
                    COMPANY_LOGO_PATH,
                    width=35 * mm,
                    height=15 * mm,
                )
            )
        except Exception:
            pass
    header_left.append(
        Paragraph(f"<b>{COMPANY_NAME}</b>", TitleStyle)
    )
    if COMPANY_ADDRESS:
        header_left.append(
            Paragraph(COMPANY_ADDRESS, base["Normal"])
        )

    period_str = (
        f"{month_name[payslip.get('month', 1)]} "
        f"{payslip.get('year', '')}"
    )
    header_right = [
        Paragraph(
            f"<b>Payslip — {period_str}</b>",
            H2Style,
        ),
    ]

    header_table = Table(
        [[header_left, header_right]],
        colWidths=[110 * mm, 65 * mm],
    )
    header_table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ])
    )
    elements.append(header_table)
    elements.append(Spacer(1, 4 * mm))

    # ----- Employee + identity block -----
    emp_rows = [
        [
            "Employee Name",
            user.get("name") or "",
            "PAN",
            payslip.get("panNumber") or "—",
        ],
        [
            "Employee Code",
            user.get("employeeCode") or "—",
            "UAN",
            payslip.get("uanNumber") or "—",
        ],
        [
            "Joining Date",
            user.get("joiningDate") or "—",
            "TDS Regime",
            payslip.get("tdsRegime") or "NEW",
        ],
        [
            "Bank",
            payslip.get("bankName") or "—",
            "A/C No.",
            payslip.get("bankAccountNumber") or "—",
        ],
        [
            "IFSC",
            payslip.get("bankIfsc") or "—",
            "Email",
            user.get("email") or "—",
        ],
    ]
    emp_table = Table(
        emp_rows,
        colWidths=[35 * mm, 55 * mm, 30 * mm, 55 * mm],
    )
    emp_table.setStyle(
        TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
            ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
            ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
    )
    elements.append(emp_table)
    elements.append(Spacer(1, 4 * mm))

    # ----- Attendance summary -----
    elements.append(
        Paragraph("<b>Attendance Summary</b>", H2Style)
    )
    att_rows = [
        [
            "Working Days",
            "Attended",
            "LOP Days",
            "LOP Deduction",
        ],
        [
            str(payslip.get("workingDays", 0)),
            str(payslip.get("attendedDays", 0)),
            str(payslip.get("lopDays", 0)),
            _money(payslip.get("lopDeduction", 0)),
        ],
    ]
    att_table = Table(
        att_rows,
        colWidths=[
            43.75 * mm,
            43.75 * mm,
            43.75 * mm,
            43.75 * mm,
        ],
    )
    att_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    elements.append(att_table)
    elements.append(Spacer(1, 4 * mm))

    # ----- Earnings + Deductions side-by-side -----
    earnings = [
        ["Earnings", "Amount"],
        ["Basic", _money(payslip.get("basic"))],
        ["HRA", _money(payslip.get("hra"))],
        [
            "Communication Allowance",
            _money(payslip.get("communicationAllowance")),
        ],
        [
            "Other Allowance",
            _money(payslip.get("otherAllowance")),
        ],
        ["Employer PF", _money(payslip.get("employerPF"))],
        [
            "Employer Insurance",
            _money(payslip.get("employerInsurance")),
        ],
        ["Total Gross", _money(payslip.get("totalGross"))],
    ]
    deductions = [
        ["Deductions", "Amount"],
        ["Employee PF", _money(payslip.get("employeePF"))],
        [
            "Professional Tax",
            _money(payslip.get("professionalTax")),
        ],
        ["TDS", _money(payslip.get("tds"))],
        [
            "Employee Insurance",
            _money(payslip.get("employeeInsurance")),
        ],
        ["", ""],
        ["", ""],
        [
            "Total Deductions",
            _money(payslip.get("totalDeductions")),
        ],
    ]

    body_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, -1), (-1, -1), colors.whitesmoke),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    earn_table = Table(
        earnings,
        colWidths=[55 * mm, 30 * mm],
    )
    earn_table.setStyle(body_style)
    ded_table = Table(
        deductions,
        colWidths=[55 * mm, 30 * mm],
    )
    ded_table.setStyle(body_style)

    side_by_side = Table(
        [[earn_table, ded_table]],
        colWidths=[87.5 * mm, 87.5 * mm],
    )
    side_by_side.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])
    )
    elements.append(side_by_side)
    elements.append(Spacer(1, 4 * mm))

    # ----- Net Pay -----
    net_table = Table(
        [
            [
                "Net Pay",
                _money(payslip.get("netPay")),
            ]
        ],
        colWidths=[120 * mm, 55 * mm],
    )
    net_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f6feb")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 12),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ])
    )
    elements.append(net_table)

    elements.append(Spacer(1, 8 * mm))
    elements.append(
        Paragraph(
            "This is a system-generated payslip and does not "
            "require a signature.",
            FooterStyle,
        )
    )
    if payslip.get("notes"):
        elements.append(Spacer(1, 3 * mm))
        elements.append(
            Paragraph(
                f"Notes: {payslip['notes']}",
                SmallStyle,
            )
        )

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
                    width=40 * mm,
                    height=18 * mm,
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

    today_str = datetime.now().strftime("%B %d, %Y")
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
