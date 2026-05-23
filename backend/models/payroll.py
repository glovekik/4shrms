from pydantic import BaseModel
from typing import Optional, Literal


TdsRegime = Literal["OLD", "NEW"]
PayrollStatus = Literal["DRAFT", "PROCESSED", "LOCKED"]


# ================= SALARY STRUCTURE =================
class SalaryStructureCreate(BaseModel):
    """All amounts are monthly, in INR (rupees)."""

    # Earnings
    basic: float
    hra: float = 0.0
    communicationAllowance: float = 0.0
    otherAllowance: float = 0.0
    employerInsurance: float = 0.0

    # Deductions (HR-entered)
    professionalTax: float = 0.0
    tds: float = 0.0
    employeeInsurance: float = 0.0

    # PF — null means auto-compute from basic with ₹15k EPF wage cap
    # (max 1800 each side). Set a number to override.
    employerPF: Optional[float] = None
    employeePF: Optional[float] = None

    # Identity / payment details (plaintext for demo — encrypt for prod)
    panNumber: Optional[str] = None
    uanNumber: Optional[str] = None
    bankAccountNumber: Optional[str] = None
    bankIfsc: Optional[str] = None
    bankName: Optional[str] = None

    tdsRegime: TdsRegime = "NEW"


# ================= PAYROLL RUN =================
class PayrollRunCreate(BaseModel):
    year: int
    month: int                # 1..12
    workingDays: int = 22     # standard 5-day week default


# ================= PAYSLIP OVERRIDE =================
class PayslipOverride(BaseModel):
    """All fields optional partial override on an individual payslip."""
    basic: Optional[float] = None
    hra: Optional[float] = None
    communicationAllowance: Optional[float] = None
    otherAllowance: Optional[float] = None
    employerPF: Optional[float] = None
    employerInsurance: Optional[float] = None

    employeePF: Optional[float] = None
    professionalTax: Optional[float] = None
    tds: Optional[float] = None
    employeeInsurance: Optional[float] = None

    lopDays: Optional[float] = None
    # HR can rewrite working/attended days at the per-employee level
    # before locking the run — useful when an employee joined mid-month.
    workingDays: Optional[float] = None
    attendedDays: Optional[float] = None
    notes: Optional[str] = None
