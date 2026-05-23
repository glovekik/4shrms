from pydantic import BaseModel
from typing import Optional, Literal


AssetStatus = Literal[
    "AVAILABLE",
    "ASSIGNED",
    "DAMAGED",
    "LOST",
    "RETIRED",
]


ReportType = Literal["DAMAGE", "LOSS", "OTHER"]


class AssetCreate(BaseModel):
    code: str            # unique asset tag, e.g. "LAP-0042"
    name: str            # e.g. "MacBook Pro 14"
    category: str        # free string, e.g. "LAPTOP", "MONITOR", "ACCESSORY"
    serialNumber: Optional[str] = None
    notes: Optional[str] = ""
    purchaseDate: Optional[str] = None  # YYYY-MM-DD
    purchasePrice: Optional[float] = None


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    serialNumber: Optional[str] = None
    notes: Optional[str] = None
    purchaseDate: Optional[str] = None
    purchasePrice: Optional[float] = None
    # Manual override of status (e.g. mark RETIRED).
    status: Optional[AssetStatus] = None


class AssetAssign(BaseModel):
    userId: str
    notes: Optional[str] = ""


class AssetReturn(BaseModel):
    notes: Optional[str] = ""
    # Status to set on the asset after return; usually AVAILABLE.
    status: AssetStatus = "AVAILABLE"


class AssetIssueReport(BaseModel):
    reportType: ReportType
    description: str


class AssetReportResolution(BaseModel):
    action: Literal["RESOLVE", "REJECT"]
    resolution: Optional[str] = ""
    # If RESOLVE, optionally update the underlying asset's status.
    newAssetStatus: Optional[AssetStatus] = None
