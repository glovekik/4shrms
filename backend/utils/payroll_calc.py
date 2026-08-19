"""Payroll calculation helpers — India FY2025-26 conventions.

Keep this small and explicit. Intentionally NOT auto-computing TDS
(slabs/regimes are HR's responsibility for now).
"""

# EPF wage ceiling: PF is computed on min(basic, 15000) at 12% each side.
PF_BASIC_CAP = 15000.0
PF_RATE = 0.12
PF_MAX_PER_SIDE = PF_BASIC_CAP * PF_RATE  # ₹1800


def auto_pf(basic: float) -> float:
    """Statutory PF on basic, capped at ₹1800."""
    if basic <= 0:
        return 0.0
    return round(min(basic, PF_BASIC_CAP) * PF_RATE, 2)


def breakdown_from_ctc(monthly_ctc: float) -> dict:
    """Standard breakdown from monthly CTC per company formula.

    Basic 50%, HRA 20%, Communication 5%, Other 19%, Employer PF 6%
    (capped at ₹1800). PF cap rollover lands in Other Allowance so the
    parts always sum to CTC.
    """
    if monthly_ctc <= 0:
        return {
            "basic": 0.0,
            "hra": 0.0,
            "communicationAllowance": 0.0,
            "otherAllowance": 0.0,
            "employerPF": 0.0,
        }
    basic = round(monthly_ctc * 0.5, 2)
    hra = round(monthly_ctc * 0.2, 2)
    communication = round(monthly_ctc * 0.05, 2)
    raw_pf = round(monthly_ctc * 0.06, 2)
    employer_pf = min(raw_pf, PF_MAX_PER_SIDE)
    other = round(monthly_ctc - basic - hra - communication - employer_pf, 2)
    return {
        "basic": basic,
        "hra": hra,
        "communicationAllowance": communication,
        "otherAllowance": max(0.0, other),
        "employerPF": employer_pf,
    }


def resolve_structure_amounts(structure: dict) -> dict:
    """Resolve nullable PF fields in a salary structure to concrete amounts.

    Mutates a shallow copy. Returns the same dict for convenience.
    """
    s = dict(structure)
    basic = float(s.get("basic", 0) or 0)
    auto = auto_pf(basic)

    if s.get("employerPF") is None:
        s["employerPF"] = auto
    if s.get("employeePF") is None:
        s["employeePF"] = auto

    return s


def compute_totals(s: dict) -> dict:
    """Split a resolved structure into cash pay, employer cost and net.

    `totalGross` is CASH ONLY — what the employee is actually paid. Employer
    PF and employer-paid insurance are cost-to-company, not money that
    reaches the employee, so they are reported separately as `employerCost`
    and combined into `totalCTC`.

    This used to fold employer contributions into `totalGross`, which had two
    consequences: the salary list showed a net well above the real bank
    credit, and — worse — LOP was priced off that inflated gross, so one day
    of loss-of-pay deducted more than one day of actual cash pay.

    Anything that genuinely wants the CTC figure should read `totalCTC`.
    """
    cash_keys = (
        "basic",
        "hra",
        "communicationAllowance",
        "otherAllowance",
    )
    employer_keys = (
        "employerPF",
        "employerInsurance",
    )
    deduction_keys = (
        "employeePF",
        "professionalTax",
        "tds",
        "employeeInsurance",
    )

    total_gross = sum(float(s.get(k, 0) or 0) for k in cash_keys)
    employer_cost = sum(float(s.get(k, 0) or 0) for k in employer_keys)
    total_deductions = sum(
        float(s.get(k, 0) or 0) for k in deduction_keys
    )

    return {
        "totalGross": round(total_gross, 2),
        "employerCost": round(employer_cost, 2),
        "totalCTC": round(total_gross + employer_cost, 2),
        "totalDeductions": round(total_deductions, 2),
        "netPay": round(total_gross - total_deductions, 2),
    }


def days_in_month(year: int, month: int) -> int:
    """Calendar days in the given month."""
    if month == 12:
        from datetime import date as _date
        return (
            _date(year + 1, 1, 1) - _date(year, month, 1)
        ).days
    from datetime import date as _date
    return (
        _date(year, month + 1, 1) - _date(year, month, 1)
    ).days


def compute_lop_deduction(
    monthly_gross: float,
    working_days: float,
    lop_days: float,
) -> float:
    """Loss-of-pay deduction: gross / workingDays * lopDays.

    Accepts fractional working_days/lop_days because HR overrides allow
    mid-month joiners (e.g. workingDays=12.5 for an employee who joined
    mid-fortnight). Clamped to the gross — a payslip can be zeroed by LOP
    but not pushed negative.
    """
    if working_days <= 0 or lop_days <= 0 or monthly_gross <= 0:
        return 0.0
    raw = (monthly_gross / working_days) * lop_days
    return round(min(raw, monthly_gross), 2)
