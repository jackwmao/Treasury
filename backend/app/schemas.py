from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import AliasChoices, BaseModel, Field


class ReviewStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class BeverageType(str, Enum):
    WINE = "wine"
    DISTILLED_SPIRITS = "distilled_spirits"
    MALT_BEVERAGE = "malt_beverage"
    UNKNOWN = "unknown"


class BoundingBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class TextToken(BaseModel):
    text: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    bbox: Optional[BoundingBox] = None


class FieldExtraction(BaseModel):
    value: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LabelEvidence(BaseModel):
    brand_name: FieldExtraction = Field(default_factory=FieldExtraction)
    class_type: FieldExtraction = Field(default_factory=FieldExtraction)
    abv: FieldExtraction = Field(default_factory=FieldExtraction)
    net_contents: FieldExtraction = Field(default_factory=FieldExtraction)
    address: FieldExtraction = Field(default_factory=FieldExtraction)
    government_warning: FieldExtraction = Field(default_factory=FieldExtraction)
    raw_text: str = ""
    tokens: List[TextToken] = Field(default_factory=list)
    low_confidence: bool = False
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_checks: Dict[str, float] = Field(default_factory=dict)


class ApplicationEvidence(BaseModel):
    domestic: Optional[bool] = None
    imported: Optional[bool] = None
    wine: Optional[bool] = None
    distilled_spirits: Optional[bool] = None
    malt_beverages: Optional[bool] = None
    source_of_product: Optional[str] = None
    brand_name: Optional[str] = None
    fanciful_name: Optional[str] = None
    grape_varietals: Optional[str] = None
    wine_appellation: Optional[str] = None
    bottler_name_address: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("bottler_name_address", "address"),
        serialization_alias="bottler_name_address",
    )
    beverage_type: BeverageType = BeverageType.UNKNOWN
    raw_text: str = ""
    raw_pdfplumber_text: str = ""


class ReconciliationResult(BaseModel):
    brand_match_score: float = 0.0
    address_match_score: float = 0.0
    field_match_scores: Dict[str, float] = Field(default_factory=dict)


class ComplianceFinding(BaseModel):
    cfr_part: str
    cfr_section: str
    code: str
    status: ReviewStatus
    message: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class VerificationResult(BaseModel):
    status: ReviewStatus
    beverage_type: BeverageType
    findings: List[ComplianceFinding] = Field(default_factory=list)
    reconciliation: ReconciliationResult
    label_evidence: LabelEvidence
    application_evidence: ApplicationEvidence


class BatchResultItem(BaseModel):
    filename: str
    result: VerificationResult
