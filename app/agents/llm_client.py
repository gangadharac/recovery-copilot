import os
import json
import logging
from typing import Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)

class LLMClient:
    """
    LLM Client supporting live Claude-3.5-Haiku via Anthropic SDK
    with an intelligent, deterministic local mock fallback for offline/zero-setup evaluation.
    """
    def __init__(self):
        self.api_key = settings.ANTHROPIC_API_KEY.strip() if settings.ANTHROPIC_API_KEY else ""
        self.client = None
        if settings.is_anthropic_configured:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=self.api_key)
                logger.info("Initialized live Anthropic Claude client.")
            except Exception as e:
                logger.warning(f"Failed to initialize Anthropic client: {e}. Falling back to mock engine.")
                self.client = None
        else:
            logger.info("No ANTHROPIC_API_KEY found. Operating in intelligent local mock mode.")

    def is_live(self) -> bool:
        return self.client is not None

    def diagnose_ambiguous_failure(self, transaction_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Diagnoses ambiguous payment failures using Claude or intelligent reasoning fallback.
        Returns: { "root_cause": str, "confidence": float, "reasoning": str, "diagnostic_factors": list }
        """
        prompt = f"""You are an expert payment reliability engineer at Razorpay analyzing a failed payment transaction.

Transaction Context:
- Transaction ID: {transaction_context.get('transaction_id')}
- Amount: INR {transaction_context.get('amount')}
- Payment Method: {transaction_context.get('payment_method')} ({transaction_context.get('payment_method_details')})
- Raw Error Code: {transaction_context.get('error_code')}
- Error Description: {transaction_context.get('error_description')}
- Error Source: {transaction_context.get('error_source')}
- Customer History: {transaction_context.get('customer_history')}
- Raw Gateway Response: {transaction_context.get('raw_gateway_response')}

Your task:
Analyze this ambiguous failure and map it to exactly ONE of the following closed root cause categories:
- bank_timeout
- insufficient_funds
- wrong_otp
- card_expired
- risk_blocked
- network_glitch
- unknown

Respond ONLY with valid JSON in this exact structure:
{{
  "root_cause": "<category>",
  "confidence": <float between 0.5 and 0.98>,
  "reasoning": "<concise explanation of why this root cause was determined>",
  "diagnostic_factors": ["<factor 1>", "<factor 2>"]
}}
"""
        if self.is_live():
            try:
                response = self.client.messages.create(
                    model=settings.CLAUDE_MODEL,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}]
                )
                text = response.content[0].text.strip()
                # Clean possible markdown formatting around json
                if text.startswith("```json"):
                    text = text[7:]
                if text.startswith("```"):
                    text = text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                return json.loads(text.strip())
            except Exception as e:
                logger.warning(f"Claude API call failed ({e}). Using intelligent mock fallback.")

        # Intelligent Mock Fallback
        return self._mock_diagnose(transaction_context)

    def generate_hinglish_nudge(self, nudge_context: Dict[str, Any]) -> str:
        """
        Generates personalized, high-converting Hinglish WhatsApp/SMS recovery messages
        matching tone to the failure reason and customer profile.
        """
        customer_name = nudge_context.get("customer_name", "Customer")
        amount = nudge_context.get("amount", "0")
        root_cause = nudge_context.get("root_cause", "unknown")
        bank = nudge_context.get("bank_code", "Bank")
        payment_method = nudge_context.get("payment_method", "payment")
        pay_url = f"https://rzp.io/i/{nudge_context.get('transaction_id', 'pay')[-6:]}"

        prompt = f"""You are Razorpay's AI Recovery Assistant. Generate a polite, clear, high-converting Hinglish WhatsApp nudge message.
Customer Name: {customer_name}
Amount: INR {amount}
Root Cause: {root_cause}
Bank: {bank}
Payment Method: {payment_method}
Payment Link: {pay_url}

Guidelines:
- Professional yet friendly Hinglish (Hindi + English blend)
- Acknowledge that the payment failed due to {root_cause} without causing panic
- Include the exact payment retry link
- Keep under 60 words.

Return ONLY the message text.
"""
        if self.is_live():
            try:
                response = self.client.messages.create(
                    model=settings.CLAUDE_MODEL,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                return response.content[0].text.strip()
            except Exception as e:
                logger.warning(f"Claude API nudge generation failed ({e}). Using intelligent mock fallback.")

        # Intelligent Mock Nudge Fallback
        return self._mock_nudge(customer_name, amount, root_cause, bank, pay_url)

    def _mock_diagnose(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic, intelligent heuristic fallback for ambiguous errors."""
        desc = (ctx.get("error_description") or "").lower()
        code = (ctx.get("error_code") or "").lower()
        method = ctx.get("payment_method") or ""
        history = ctx.get("customer_history") or {}
        raw = ctx.get("raw_gateway_response") or {}
        raw_status = str(raw.get("status_code", ""))

        if "timeout" in desc or "switch" in desc or raw_status == "504":
            return {
                "root_cause": "bank_timeout",
                "confidence": 0.91,
                "reasoning": f"Gateway reported HTTP {raw_status} and switch latency; downstream issuer timed out.",
                "diagnostic_factors": ["Gateway response 504", "Switch timeout pattern", "Bank latency spike"]
            }
        elif "balance" in desc or "limit" in desc or "insufficient" in desc:
            return {
                "root_cause": "insufficient_funds",
                "confidence": 0.88,
                "reasoning": "Issuer declined due to insufficient available funds / card spending limit.",
                "diagnostic_factors": ["Balance/limit keywords in error payload", "Issuer card decline"]
            }
        elif "otp" in desc or "3ds" in desc or "auth" in desc:
            return {
                "root_cause": "wrong_otp",
                "confidence": 0.89,
                "reasoning": "Customer failed 3D-Secure two-factor authentication challenge within the window.",
                "diagnostic_factors": ["3DS verification failure", "OTP expired during checkout"]
            }
        elif "expired" in desc or "validity" in desc:
            return {
                "root_cause": "card_expired",
                "confidence": 0.95,
                "reasoning": "Instrument expired; issuer rejected card authorization.",
                "diagnostic_factors": ["Card expiration date in past", "Invalid instrument metadata"]
            }
        elif "risk" in desc or "fraud" in desc or "velocity" in desc or history.get("has_disputed_charges"):
            return {
                "root_cause": "risk_blocked",
                "confidence": 0.93,
                "reasoning": "Flagged by fraud prevention heuristics or high velocity threshold.",
                "diagnostic_factors": ["Velocity check triggered", "Risk engine rule block"]
            }
        elif "network" in desc or "packet" in desc or "disconnect" in desc:
            return {
                "root_cause": "network_glitch",
                "confidence": 0.86,
                "reasoning": "Transient client/network socket drop during checkout negotiation.",
                "diagnostic_factors": ["Socket disconnect", "Client drop before gateway ACK"]
            }
        else:
            # Contextual deduction based on payment method and reliability
            if method == "upi":
                return {
                    "root_cause": "network_glitch",
                    "confidence": 0.78,
                    "reasoning": "Ambiguous UPI decline with high customer reliability score indicates transient NPCI/PSP network lag.",
                    "diagnostic_factors": ["UPI PSP timeout", "High customer reliability history"]
                }
            else:
                return {
                    "root_cause": "bank_timeout",
                    "confidence": 0.75,
                    "reasoning": "Generic gateway decline on card instrument mapped to issuer switch unavailability.",
                    "diagnostic_factors": ["Generic 500 decline", "Issuer gateway unavailability"]
                }

    def _mock_nudge(self, name: str, amount: Any, root_cause: str, bank: str, pay_url: str) -> str:
        """Generates realistic few-shot Hinglish WhatsApp recovery messages."""
        if root_cause == "insufficient_funds":
            return (
                f"Namaste {name}! Aapka INR {amount} ka payment complete nahi ho paya. "
                f"Aap alternate bank account ya UPI se bina kisi delay ke retry kar sakte hain 👉 {pay_url}"
            )
        elif root_cause == "wrong_otp":
            return (
                f"Namaste {name}! Aapka INR {amount} ka payment OTP mismatch/timeout ki wajah se pause ho gaya. "
                f"Bas 1 click mein naya OTP mangwayein aur payment complete karein 👉 {pay_url}"
            )
        elif root_cause == "bank_timeout":
            return (
                f"Hi {name}! {bank} ke server pe temporary downtime ki wajah se aapka INR {amount} ka payment ruk gaya. "
                f"Server issue ab resolve ho chuka hai, yahan se instant complete karein 👉 {pay_url}"
            )
        elif root_cause == "card_expired":
            return (
                f"Namaste {name}! Aapka card expire hone ke karan INR {amount} ka transaction complete nahi hua. "
                f"Aap UPI ya doosre card se turant pay kar sakte hain 👉 {pay_url}"
            )
        else:
            return (
                f"Namaste {name}! Aapka INR {amount} ka payment complete nahi ho paya. "
                f"Aap niche diye gaye link se turant retry kar sakte hain 👉 {pay_url}"
            )

# Global LLM client singleton
llm_client = LLMClient()
