from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

class PaymentMethod(str, Enum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"

class PaymentMethodDetails(BaseModel):
    card_network: Optional[str] = None       # e.g., "Visa", "Mastercard", "RuPay"
    card_type: Optional[str] = None          # e.g., "credit", "debit"
    bank_code: Optional[str] = None          # e.g., "HDFC", "ICICI", "SBI", "AXIS", "KOTAK"
    upi_vpa: Optional[str] = None            # e.g., "aarav@okhdfcbank"

class CustomerHistory(BaseModel):
    lifetime_successful_transactions: int = Field(default=0, ge=0)
    lifetime_failed_transactions: int = Field(default=0, ge=0)
    preferred_payment_method: str = "upi"
    reliability_score: float = Field(default=0.85, ge=0.0, le=1.0)
    has_disputed_charges: bool = False

class RawGatewayResponse(BaseModel):
    status_code: Optional[str] = None
    issuer_code: Optional[str] = None
    decline_category: Optional[str] = None

class Transaction(BaseModel):
    transaction_id: str
    customer_id: str
    customer_name: str
    customer_phone: str
    customer_email: str
    amount: float = Field(gt=0)
    currency: str = "INR"
    payment_method: PaymentMethod
    payment_method_details: PaymentMethodDetails = Field(default_factory=PaymentMethodDetails)
    error_code: str
    error_description: str
    error_source: str                         # e.g., "bank_switch", "user_error", "risk_engine", "network", "gateway"
    retry_count: int = Field(default=0, ge=0)
    last_retry_timestamp: Optional[datetime] = None
    timestamp: datetime
    auto_charge_consent: bool = False         # Required for auto-charge guardrail
    merchant_id: str = "merch_acme_retail_in"
    customer_history: CustomerHistory = Field(default_factory=CustomerHistory)
    raw_gateway_response: Optional[RawGatewayResponse] = None
