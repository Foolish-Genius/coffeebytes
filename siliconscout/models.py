"""Pydantic schemas. extra='ignore' + defaults keep old checkpoints readable after config changes."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GV(str, Enum):  # gate value
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    TO_CONFIRM = "TO_CONFIRM"
    NA = "N/A"


class Role(str, Enum):
    MAKER = "MAKER"
    BRAND_OWNER = "BRAND_OWNER"
    AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
    TRADER = "TRADER"
    UNKNOWN = "UNKNOWN"


class Decision(str, Enum):
    RECOMMEND_ORDER = "RECOMMEND_ORDER"
    QUALIFIED_FOR_RFQ = "QUALIFIED_FOR_RFQ"
    PROVISIONAL = "PROVISIONAL"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    CANNOT_VERIFY = "CANNOT_VERIFY"
    NOT_A_FIT = "NOT_A_FIT"


class PriceLabel(str, Enum):
    SUPPLIER_QUOTE = "SUPPLIER_QUOTE"
    DISTRIBUTOR_LIVE = "DISTRIBUTOR_LIVE"
    PUBLIC_BUDGETARY = "PUBLIC_BUDGETARY"
    TEST_FIXTURE = "TEST_FIXTURE"


ORDER_LABELS = {PriceLabel.SUPPLIER_QUOTE, PriceLabel.DISTRIBUTOR_LIVE}


class Spec(_M):
    raw_need: str
    lane: str
    device: str
    quantity: Optional[int] = None
    unit: str = "units"
    delivery_location: Optional[str] = None
    mpn: Optional[str] = None
    requirements: dict[str, str] = Field(default_factory=dict)  # field -> label
    assumptions: list[str] = Field(default_factory=list)
    source: str = "rules"  # rules | llm


class Page(_M):
    url: str
    text: str
    fetched_at: str = ""
    sha256: str = ""


class Claim(_M):
    field: str
    value: str = ""
    passage: str
    url: str = ""
    verified: Optional[bool] = None
    reason: str = ""


class Candidate(_M):
    candidate_id: str
    company: str
    domain: str = ""
    model: str = ""
    listing_role: str = ""
    email: str = ""
    pages: list[Page] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    source: str = "fixture"


class RegistryHit(_M):
    registry: str
    title: str
    as_on: str
    sha256: str
    label: str
    company: str
    model: str
    ref: str
    valid_until: str = ""
    address: str = ""
    brand: str = ""


class GateResult(_M):
    gate: str
    value: GV
    reason: str
    source: str = ""
    passage: str = ""


class Quote(_M):
    candidate_id: str
    label: PriceLabel
    currency: str = "INR"
    unit_price: Optional[float] = None
    quantity: Optional[int] = None
    moq: Optional[int] = None
    approval_ref: Optional[str] = None  # TAC / certificate number for the quoted model
    model: Optional[str] = None
    warranty_months: Optional[int] = None
    service_network: Optional[str] = None
    delivery_days: Optional[int] = None
    payment_terms: Optional[str] = None
    validity_until: Optional[str] = None
    gst_pct: Optional[float] = None
    freight: Optional[float] = None
    installation: Optional[float] = None
    # component lane
    mpn: Optional[str] = None
    packaging: Optional[str] = None
    pack_multiple: Optional[int] = None
    stock: Optional[int] = None
    price_break_qty: Optional[int] = None
    lead_time_days: Optional[int] = None
    received_at: str = ""
    source: str = ""

    def goods_value(self) -> Optional[float]:
        if self.unit_price is None or self.quantity is None:
            return None
        return round(self.unit_price * self.quantity, 2)


class Assessment(_M):
    candidate_id: str
    company: str
    model: str = ""
    role: Role = Role.UNKNOWN
    maker_of_record: Optional[str] = None
    decision: Decision = Decision.CANNOT_VERIFY
    gates: list[GateResult] = Field(default_factory=list)
    registry_hits: list[RegistryHit] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    quote: Optional[Quote] = None
    rank: Optional[int] = None
    tied_with: list[str] = Field(default_factory=list)

    def gate(self, name: str) -> Optional[GateResult]:
        return next((g for g in self.gates if g.gate == name), None)


class RFQ(_M):
    rfq_id: str
    candidate_id: str
    to: str
    subject: str
    body: str
    status: str = "DRAFT"  # DRAFT | APPROVED | SENT | HELD_AMBIGUOUS
    idempotency_key: str = ""
    approved_by: Optional[str] = None
    sent_at: Optional[str] = None


class JobState(_M):
    job_id: str
    status: str = "NEW"
    stage: str = "NEW"
    spec: Optional[Spec] = None
    candidates: list[Candidate] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)
    rfqs: list[RFQ] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
