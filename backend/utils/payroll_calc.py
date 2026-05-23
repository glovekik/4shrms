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
    """Compute totalGross / totalDeductions / netPay from a resolved structure."""
    earnings_keys = (
        "basic",
        "hra",
        "communicationAllowance",
        "otherAllowance",
        "employerPF",
        "employerInsurance",
    )
    deduction_keys = (
        "employeePF",
        "professionalTax",
        "tds",
        "employeeInsurance",
    )

    total_gross = sum(float(s.get(k, 0) or 0) for k in earnings_keys)
    total_deductions = sum(
        float(s.get(k, 0) or 0) for k in deduction_keys
    )

    return {
        "totalGross": round(total_gross, 2),
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
