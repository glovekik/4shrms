from pydantic import BaseModel, EmailStr
from typing import Optional, Literal


# Job designation — purely informational, separate from `role` (which controls
# permissions: HR / MANAGER / USER). Free-text by product spec — HR can type
# anything (e.g. "Senior Engineer", "Founder", "Intern"). Kept as a plain str
# so legacy data with the old fixed values keeps working without migration.
UserTag = str


UserStatus = Literal[
    "Active",
    "Inactive",
    "OnLeave",
    "Terminated",
]


UserRole = Literal["HR", "MANAGER", "USER", "CEO"]

# Roles HR can grant via the app (excludes CEO — that one needs a script).
GrantableRole = Literal["HR", "MANAGER", "USER"]


# ================= Profile sub-models =================
WeekdayLocation = Literal["Home", "Office", "Other"]


class UsualWorkLocation(BaseModel):
    monday: Optional[WeekdayLocation] = None
    tuesday: Optional[WeekdayLocation] = None
    wednesday: Optional[WeekdayLocation] = None
    thursday: Optional[WeekdayLocation] = None
    friday: Optional[WeekdayLocation] = None
    saturday: Optional[WeekdayLocation] = None
    sunday: Optional[WeekdayLocation] = None


class WorkInfo(BaseModel):
    departmentId: Optional[str] = None
    jobPosition: Optional[str] = None
    jobTitle: Optional[str] = None
    reportingManagerId: Optional[str] = None
    projectManagerIds: Optional[list[str]] = None
    workAddress: Optional[str] = None
    workLocation: Optional[str] = None
    usualWorkLocation: Optional[UsualWorkLocation] = None
    notes: Optional[str] = None


class PrivateAddress(BaseModel):
    street1: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pinCode: Optional[str] = None
    country: Optional[str] = None


CertificationLevel = Literal[
    "Graduate", "Bachelor", "Master", "Doctor", "Other"
]


class Education(BaseModel):
    certificationLevel: Optional[CertificationLevel] = None
    fieldOfStudy: Optional[str] = None


class PersonalInfo(BaseModel):
    personalEmail: Optional[EmailStr] = None
    phone: Optional[str] = None
    legalName: Optional[str] = None
    birthday: Optional[str] = None  # YYYY-MM-DD
    placeOfBirth: Optional[str] = None
    gender: Optional[str] = None
    disabled: Optional[bool] = None
    bloodGroup: Optional[str] = None
    maritalStatus: Optional[str] = None
    address: Optional[PrivateAddress] = None
    education: Optional[Education] = None


class BankAccount(BaseModel):
    bankName: Optional[str] = None
    accountNumber: Optional[str] = None
    ifscCode: Optional[str] = None
    branch: Optional[str] = None
    accountHolderName: Optional[str] = None


class EmergencyContact(BaseModel):
    contactName: Optional[str] = None
    relationship: Optional[str] = None
    phone: Optional[str] = None


class EmployeeDocuments(BaseModel):
    """File URLs (uploads handled out-of-band). All optional."""
    idCardCopy: Optional[str] = None
    aadhaarCopy: Optional[str] = None
    panCopy: Optional[str] = None
    tenth: Optional[str] = None
    inter: Optional[str] = None
    ug: Optional[str] = None
    pg: Optional[str] = None
    phd: Optional[str] = None
    offerLetter: Optional[str] = None
    experienceLetter: Optional[str] = None
    resume: Optional[str] = None
    passport: Optional[str] = None
    relievingLetter: Optional[str] = None
    salarySlips: Optional[list[str]] = None
    certifications: Optional[list[str]] = None


class StatutoryInfo(BaseModel):
    pan: Optional[str] = None
    uan: Optional[str] = None
    pfAccountNumber: Optional[str] = None
    esiNumber: Optional[str] = None


# Frontend canonical values:
#   wageType:      "Fixed Wage" | "Hourly Wage"
#   wageDuration:  "Year" | "Half-Year" | "Quarter" | "2 Months" | "Month" |
#                  "Half-Month" | "2 Weeks" | "Week" | "Day"
#   employeeType:  "Full-time" | "Part-time" | "Internship" | "Contract" |
#                  "Consultant"
#
# Kept as plain Optional[str] (not Literal) on purpose: the previous
# Literal enum on the backend used "Employee/Worker/Student/Trainee/..."
# which the UI never sends, so every contract save silently 422'd and
# the fields appeared blank after refresh. The UI dropdown is now the
# single source of truth for valid values.
class ContractOverview(BaseModel):
    contractStartDate: Optional[str] = None  # YYYY-MM-DD
    contractEndDate: Optional[str] = None
    wageType: Optional[str] = None
    wage: Optional[float] = None
    wageDuration: Optional[str] = None
    employeeType: Optional[str] = None


# ================= Auth models =================
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


# ================= HR-facing create/update =================
class HRCreateUser(BaseModel):
    name: str
    email: EmailStr
    password: str  # initial password set by HR; user can change later

    # Identity / header
    role: Optional[UserRole] = "USER"
    tag: Optional[UserTag] = "Employee"
    employeeCode: Optional[str] = None
    workPhone: Optional[str] = None
    joiningDate: Optional[str] = None  # YYYY-MM-DD
    status: Optional[UserStatus] = "Active"
    profilePictureUrl: Optional[str] = None

    # Org structure
    departmentId: Optional[str] = None
    reportingManagerId: Optional[str] = None
    projectManagerIds: Optional[list[str]] = None

    # Profile tabs — all optional, settable here or via PUT later
    work: Optional[WorkInfo] = None
    personal: Optional[PersonalInfo] = None
    bankAccounts: Optional[list[BankAccount]] = None
    emergencyContact: Optional[EmergencyContact] = None
    documents: Optional[EmployeeDocuments] = None
    statutory: Optional[StatutoryInfo] = None
    contract: Optional[ContractOverview] = None

    # Assets HR wants to hand the employee on day one. Each id must point
    # to an AVAILABLE asset; assignment runs after the user is inserted.
    initialAssetIds: Optional[list[str]] = None


class HRUserUpdate(BaseModel):
    """Partial update — only fields HR is allowed to change.

    Email IS allowed (HR can correct/change the login email); the route
    enforces uniqueness. Password still goes through the reset flow only.
    Role IS allowed here — HR can promote anyone to HR/MANAGER/USER.
    Any safety guard on HR promotion lives in the route, not the model.
    """
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Literal["MANAGER", "USER", "HR"]] = None
    tag: Optional[UserTag] = None
    employeeCode: Optional[str] = None
    workPhone: Optional[str] = None
    joiningDate: Optional[str] = None
    status: Optional[UserStatus] = None
    profilePictureUrl: Optional[str] = None

    # Org structure (empty string clears the field)
    departmentId: Optional[str] = None
    reportingManagerId: Optional[str] = None
    projectManagerIds: Optional[list[str]] = None

    # Profile tabs
    work: Optional[WorkInfo] = None
    personal: Optional[PersonalInfo] = None
    bankAccounts: Optional[list[BankAccount]] = None
    emergencyContact: Optional[EmergencyContact] = None
    documents: Optional[EmployeeDocuments] = None
    statutory: Optional[StatutoryInfo] = None
    contract: Optional[ContractOverview] = None

    # HR-provided reason when marking the user NOT ACTIVE (status=Terminated).
    # Stored on the user record alongside terminatedAt/terminatedBy stamped
    # by the route.
    terminationReason: Optional[str] = None
