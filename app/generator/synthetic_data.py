import json
import random
import csv
from datetime import datetime, timedelta
from typing import List, Dict, Any
from pathlib import Path
from app.config import settings
from app.schemas.transaction import (
    Transaction,
    PaymentMethod,
    PaymentMethodDetails,
    CustomerHistory,
    RawGatewayResponse,
)

# Realistic Indian Customer Names
CUSTOMER_NAMES = [
    ("Aarav Sharma", "aarav.sharma@example.com", "+919876543210"),
    ("Priya Patel", "priya.patel@example.com", "+919876543211"),
    ("Rohan Mehta", "rohan.mehta@example.com", "+919876543212"),
    ("Ananya Iyer", "ananya.iyer@example.com", "+919876543213"),
    ("Vikram Singh", "vikram.singh@example.com", "+919876543214"),
    ("Sneha Reddy", "sneha.reddy@example.com", "+919876543215"),
    ("Karan Kapoor", "karan.kapoor@example.com", "+919876543216"),
    ("Pooja Nair", "pooja.nair@example.com", "+919876543217"),
    ("Aditya Verma", "aditya.verma@example.com", "+919876543218"),
    ("Meera Joshi", "meera.joshi@example.com", "+919876543219"),
    ("Rahul Gupta", "rahul.gupta@example.com", "+919876543220"),
    ("Divya Deshmukh", "divya.d@example.com", "+919876543221"),
    ("Siddharth Rao", "sid.rao@example.com", "+919876543222"),
    ("Kavita Menon", "kavita.m@example.com", "+919876543223"),
    ("Arjun Nambiar", "arjun.n@example.com", "+919876543224"),
]

BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES_BANK", "PNB"]
CARD_NETWORKS = ["Visa", "Mastercard", "RuPay"]

# Realistic Razorpay-style Error Scenarios & Templates
FAILURE_SCENARIOS = [
    # 1. Bank Switch Timeout (High Recovery Potential)
    {
        "category": "bank_timeout",
        "error_codes": ["GATEWAY_ERROR", "BANK_TIMEOUT", "ISSUER_SWITCH_UNAVAILABLE"],
        "error_descriptions": [
            "Bank network timeout occurred while communicating with issuer switch",
            "Gateway timeout: Issuer bank took more than 30s to acknowledge debit request",
            "Temporary switch downtime at processing bank"
        ],
        "error_source": "bank_switch",
        "payment_methods": [PaymentMethod.CARD, PaymentMethod.NETBANKING, PaymentMethod.UPI],
        "gateway_status": "504",
        "issuer_code": "TIMED_OUT",
        "weight": 25
    },
    # 2. Insufficient Funds (Nudge / Alternative Method)
    {
        "category": "insufficient_funds",
        "error_codes": ["insufficient_funds", "INSUFFICIENT_BALANCE", "LIMIT_EXCEEDED"],
        "error_descriptions": [
            "Account balance insufficient to cover transaction amount",
            "Daily transaction limit exceeded on the selected debit card / account",
            "Bank declined transaction: Insufficient available credit limit"
        ],
        "error_source": "user_error",
        "payment_methods": [PaymentMethod.CARD, PaymentMethod.UPI],
        "gateway_status": "400",
        "issuer_code": "INSUFFICIENT_FUNDS",
        "weight": 20
    },
    # 3. OTP Expired / Authentication Failure
    {
        "category": "wrong_otp",
        "error_codes": ["otp_timeout", "AUTH_FAILED", "OTP_EXPIRED", "3DS_VERIFICATION_FAILED"],
        "error_descriptions": [
            "Customer failed to enter OTP within the 5-minute authentication window",
            "Incorrect 3D Secure OTP entered multiple times by customer",
            "Authentication cancelled by customer during bank challenge"
        ],
        "error_source": "user_error",
        "payment_methods": [PaymentMethod.CARD, PaymentMethod.NETBANKING],
        "gateway_status": "401",
        "issuer_code": "AUTH_FAILED",
        "weight": 20
    },
    # 4. Card Expired / Invalid Card Details
    {
        "category": "card_expired",
        "error_codes": ["card_expired", "EXPIRED_CARD", "INVALID_EXPIRY_DATE"],
        "error_descriptions": [
            "Card validity has expired (expiry date is in the past)",
            "Card issuer declined: Card expired or invalid expiration month/year",
            "Payment method not valid: Card deactivated due to expiry"
        ],
        "error_source": "user_error",
        "payment_methods": [PaymentMethod.CARD],
        "gateway_status": "400",
        "issuer_code": "CARD_EXPIRED",
        "weight": 15
    },
    # 5. Risk Engine / Fraud Block
    {
        "category": "risk_blocked",
        "error_codes": ["risk_blocked", "FRAUD_SUSPECTED", "VELOCITY_EXCEEDED", "BLACKLISTED_CARD"],
        "error_descriptions": [
            "Transaction blocked by Razorpay Shield risk engine: High velocity detected",
            "Issuer risk block: Suspicious international IP pattern or stolen card match",
            "AML compliance rule triggered: Flagged for mandatory manual review"
        ],
        "error_source": "risk_engine",
        "payment_methods": [PaymentMethod.CARD, PaymentMethod.UPI],
        "gateway_status": "403",
        "issuer_code": "RISK_BLOCK",
        "weight": 10
    },
    # 6. Network Glitch / Client Disconnect
    {
        "category": "network_glitch",
        "error_codes": ["network_error", "CONNECTION_DROPPED", "CLIENT_DISCONNECTED"],
        "error_descriptions": [
            "Network packet drop occurred during secure handshake",
            "User browser connection reset during 3DS redirect",
            "Mobile network connection lost while awaiting webhook confirmation"
        ],
        "error_source": "network",
        "payment_methods": [PaymentMethod.UPI, PaymentMethod.CARD],
        "gateway_status": "499",
        "issuer_code": "NET_ERR",
        "weight": 15
    },
    # 7. Ambiguous Edge Cases (Requires Reasoning / LLM Fallback)
    {
        "category": "ambiguous",
        "error_codes": ["BAD_REQUEST_ERROR", "GATEWAY_ERROR", "PAYMENT_DECLINED", "UNKNOWN_SWITCH_ERROR"],
        "error_descriptions": [
            "Transaction declined by downstream partner without specific error description",
            "Generic payment processor rejection: Code 9999",
            "Unexpected error occurred during multi-tender settlement negotiation"
        ],
        "error_source": "gateway",
        "payment_methods": [PaymentMethod.CARD, PaymentMethod.UPI, PaymentMethod.NETBANKING],
        "gateway_status": "500",
        "issuer_code": "GENERIC_DECLINE",
        "weight": 15
    }
]

def generate_synthetic_transactions(count: int = 125, seed: int = 42) -> List[Transaction]:
    """
    Generates realistic Razorpay failed transaction records with intentional edge cases:
    - Retries at limit (retry_count >= 3)
    - Cooldown violations (<30 mins since last retry)
    - Consent flag variations (auto_charge_consent = True/False)
    - High risk transactions
    - Ambiguous errors needing LLM reasoning
    """
    random.seed(seed)
    transactions: List[Transaction] = []
    base_time = datetime(2026, 8, 31, 10, 0, 0)
    
    # Weighted scenario choices
    scenario_weights = [s["weight"] for s in FAILURE_SCENARIOS]
    
    for i in range(1, count + 1):
        txn_id = f"txn_rzp_{100000 + i}"
        cust_idx = (i - 1) % len(CUSTOMER_NAMES)
        name, email, phone = CUSTOMER_NAMES[cust_idx]
        cust_id = f"cust_{20000 + (i % 35)}"
        
        # Pick scenario
        scenario = random.choices(FAILURE_SCENARIOS, weights=scenario_weights, k=1)[0]
        
        payment_method = random.choice(scenario["payment_methods"])
        error_code = random.choice(scenario["error_codes"])
        error_desc = random.choice(scenario["error_descriptions"])
        bank = random.choice(BANKS)
        
        # Build payment method details
        if payment_method == PaymentMethod.CARD:
            details = PaymentMethodDetails(
                card_network=random.choice(CARD_NETWORKS),
                card_type=random.choice(["credit", "debit"]),
                bank_code=bank,
                upi_vpa=None
            )
        elif payment_method == PaymentMethod.UPI:
            details = PaymentMethodDetails(
                card_network=None,
                card_type=None,
                bank_code=bank,
                upi_vpa=f"{name.lower().replace(' ', '')}@ok{bank.lower()}"
            )
        else: # Netbanking
            details = PaymentMethodDetails(
                card_network=None,
                card_type=None,
                bank_code=bank,
                upi_vpa=None
            )
            
        # Amount distribution (Retail INR 199 to High Value INR 38,500)
        amount_tier = random.choices(["small", "medium", "large"], weights=[60, 30, 10], k=1)[0]
        if amount_tier == "small":
            amount = round(random.uniform(199.0, 2499.0), 2)
        elif amount_tier == "medium":
            amount = round(random.uniform(2500.0, 9999.0), 2)
        else:
            amount = round(random.uniform(10000.0, 38500.0), 2)
            
        # Timestamp distribution across past 6 hours
        minutes_ago = random.randint(5, 360)
        txn_timestamp = base_time - timedelta(minutes=minutes_ago)
        
        # Edge Case Injections:
        # Case A: Retry count distribution (test retry limits)
        # 10% will be retry_count = 3 (at limit), 5% retry_count = 4 (over limit)
        retry_rand = random.random()
        if retry_rand < 0.10:
            retry_count = 3
        elif retry_rand < 0.15:
            retry_count = 4
        elif retry_rand < 0.40:
            retry_count = 1
        elif retry_rand < 0.55:
            retry_count = 2
        else:
            retry_count = 0
            
        # Case B: Last retry timestamp (test cooldown rule of 30 min)
        last_retry_ts = None
        if retry_count > 0:
            # 30% of retries happened very recently (< 25 min ago)
            if random.random() < 0.30:
                last_retry_ts = txn_timestamp - timedelta(minutes=random.randint(5, 25))
            else:
                last_retry_ts = txn_timestamp - timedelta(minutes=random.randint(45, 180))
                
        # Case C: Auto-charge consent flag (test consent guardrail)
        # Only ~35% of customers have active auto-charge mandate consent
        auto_charge_consent = random.random() < 0.35
        
        # Case D: Customer history (used for personalization & LLM fallback reasoning)
        past_success = random.randint(1, 28)
        past_fail = random.randint(0, 4)
        reliability = round(past_success / (past_success + past_fail + 0.1), 2)
        customer_history = CustomerHistory(
            lifetime_successful_transactions=past_success,
            lifetime_failed_transactions=past_fail,
            preferred_payment_method=random.choice(["upi", "card", "netbanking"]),
            reliability_score=reliability,
            has_disputed_charges=(scenario["category"] == "risk_blocked" and random.random() < 0.4)
        )
        
        raw_gateway = RawGatewayResponse(
            status_code=scenario["gateway_status"],
            issuer_code=scenario["issuer_code"],
            decline_category=scenario["category"]
        )
        
        transaction = Transaction(
            transaction_id=txn_id,
            customer_id=cust_id,
            customer_name=name,
            customer_phone=phone,
            customer_email=email,
            amount=amount,
            currency="INR",
            payment_method=payment_method,
            payment_method_details=details,
            error_code=error_code,
            error_description=error_desc,
            error_source=scenario["error_source"],
            retry_count=retry_count,
            last_retry_timestamp=last_retry_ts,
            timestamp=txn_timestamp,
            auto_charge_consent=auto_charge_consent,
            merchant_id=settings.MERCHANT_ID,
            customer_history=customer_history,
            raw_gateway_response=raw_gateway
        )
        transactions.append(transaction)
        
    return transactions

def save_synthetic_data(transactions: List[Transaction], json_path: Path = settings.SYNTHETIC_DATA_PATH, csv_path: Path = settings.SYNTHETIC_CSV_PATH):
    """Saves generated transactions to JSON and CSV files."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save JSON
    with open(json_path, "w", encoding="utf-8") as f:
        data = [t.model_dump(mode="json") for t in transactions]
        json.dump(data, f, indent=2)
        
    # Save CSV for easy inspection
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "transaction_id", "customer_id", "customer_name", "amount", "currency",
            "payment_method", "error_code", "error_description", "error_source",
            "retry_count", "auto_charge_consent", "reliability_score", "timestamp"
        ])
        for t in transactions:
            writer.writerow([
                t.transaction_id, t.customer_id, t.customer_name, t.amount, t.currency,
                t.payment_method.value, t.error_code, t.error_description, t.error_source,
                t.retry_count, t.auto_charge_consent, t.customer_history.reliability_score,
                t.timestamp.isoformat()
            ])

def load_synthetic_transactions(json_path: Path = settings.SYNTHETIC_DATA_PATH) -> List[Transaction]:
    """Loads transactions from JSON file."""
    if not json_path.exists():
        txns = generate_synthetic_transactions()
        save_synthetic_data(txns, json_path)
        return txns
        
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return [Transaction.model_validate(item) for item in data]

if __name__ == "__main__":
    txns = generate_synthetic_transactions(125)
    save_synthetic_data(txns)
    print(f"Successfully generated and saved {len(txns)} synthetic transactions.")
    print(f"JSON: {settings.SYNTHETIC_DATA_PATH}")
    print(f"CSV:  {settings.SYNTHETIC_CSV_PATH}")
