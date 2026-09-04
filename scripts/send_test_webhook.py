import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import hmac
import hashlib
import argparse
import requests
from app.config import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCENARIOS = {
    "otp": {
        "description": "OTP Timeout / Failure",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "OTP entered was incorrect or expired",
        "error_reason": "payment_failed",
    },
    "bank_down": {
        "description": "Bank Server Down / Downtime",
        "error_code": "GATEWAY_ERROR",
        "error_description": "Bank server down or unresponsive",
        "error_reason": "payment_failed",
    },
    "insufficient_funds": {
        "description": "Insufficient Balance / Credit Limit Exceeded (Amount Failure)",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Insufficient balance in account or card limit exceeded",
        "error_reason": "insufficient_funds",
    },
    "risk_blocked": {
        "description": "Risk Blocked / Fraud Guardrail",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Transaction blocked by risk evaluation rules",
        "error_reason": "risk_check_failed",
    },
    "network_glitch": {
        "description": "Network Glitch / Socket Timeout",
        "error_code": "GATEWAY_ERROR",
        "error_description": "Network timeout or connection reset",
        "error_reason": "network_timeout",
    },
    "card_expired": {
        "description": "Expired Card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Card expiry date has passed",
        "error_reason": "card_expired",
    },
    "unknown": {
        "description": "Unrecognized Failure Reason",
        "error_code": "INTERNAL_SERVER_ERROR",
        "error_description": "Unmapped system exception code XYZ",
        "error_reason": "unknown_error",
    }
}

def send_webhook(scenario_key: str = "insufficient_funds", txn_id: str = None, amount_inr: float = 2500.0, base_url: str = "http://localhost:8000"):
    scenario = SCENARIOS.get(scenario_key, SCENARIOS["insufficient_funds"])
    if not txn_id:
        import time
        txn_id = f"TXN_{scenario_key.upper()}_{int(time.time())}"

    paise = int(round(amount_inr * 100))

    payload = {
        "entity": "event",
        "account_id": "acc_live_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{txn_id.lower()}",
                    "amount": paise,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": scenario["error_code"],
                    "error_description": scenario["error_description"],
                    "error_reason": scenario["error_reason"],
                    "email": "customer@example.com",
                    "contact": "+919876543210",
                    "notes": {
                        "transaction_id": txn_id
                    }
                }
            }
        }
    }

    raw_body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        print("❌ Error: RAZORPAY_WEBHOOK_SECRET is not configured in .env")
        return

    # Compute HMAC-SHA256 signature
    signature = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()

    endpoint = f"{base_url.rstrip('/')}/webhooks/razorpay"
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature
    }

    print("==================================================================")
    print(f"🚀 Sending Razorpay Webhook Event: {scenario['description']}")
    print("==================================================================")
    print(f"• Endpoint:        {endpoint}")
    print(f"• Transaction ID:  {txn_id}")
    print(f"• Amount:          INR {amount_inr:,.2f} ({paise} paise)")
    print(f"• Error Code:      {scenario['error_code']}")
    print(f"• Description:     {scenario['error_description']}")
    print(f"• HMAC Signature:  {signature[:16]}...")
    print("------------------------------------------------------------------")

    try:
        res = requests.post(endpoint, data=raw_body, headers=headers, timeout=10)
        print(f"HTTP Status: {res.status_code}")
        try:
            resp_data = res.json()
            print("Response JSON:")
            print(json.dumps(resp_data, indent=2))
        except Exception:
            print("Response Text:", res.text)
    except Exception as e:
        print(f"❌ Failed to reach server: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send authenticated Razorpay test webhook")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), default="insufficient_funds", help="Failure scenario type")
    parser.add_argument("--txn", type=str, default=None, help="Custom transaction ID")
    parser.add_argument("--amount", type=float, default=2500.0, help="Transaction amount in INR")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="FastAPI server base URL")

    args = parser.parse_args()
    send_webhook(args.scenario, args.txn, args.amount, args.url)
