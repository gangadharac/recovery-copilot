import random
from typing import Dict, Any, Tuple
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import RootCause
from app.schemas.strategy import RecoveryAction

# Backup Gateway Switches used during intelligent dynamic re-routing
ALTERNATE_GATEWAY_SWITCHES = [
    "switch_hdfc_direct_v2",
    "switch_icici_mesh_v3",
    "switch_axis_super_router",
    "switch_npci_fast_lane"
]

class RazorpaySimulator:
    """
    Simulates Razorpay test-mode payment gateway execution & intelligent rerouting.
    Calculates realistic, defensible recovery outcomes based on the diagnostic category and action.
    """
    def __init__(self, seed: int = 101):
        self.rng = random.Random(seed)

    def simulate_retry(self, txn: Transaction, root_cause: RootCause) -> Tuple[bool, str, str]:
        """
        Simulates an automated payment retry through an alternate switch.
        Returns: (success: bool, gateway_switch: str, notes: str)
        """
        chosen_switch = self.rng.choice(ALTERNATE_GATEWAY_SWITCHES)
        
        # Hard Rule: Retrying an expired card with the exact same expired card ALWAYS fails
        if root_cause == RootCause.CARD_EXPIRED:
            return (
                False,
                chosen_switch,
                "Payment declined: Expired card re-attempted on alternate switch rejected by issuer."
            )
            
        # Hard Rule: Risk-blocked cannot succeed on gateway retry
        if root_cause == RootCause.RISK_BLOCKED:
            return (
                False,
                chosen_switch,
                "Payment rejected: Instrument is flagged on central risk blacklist."
            )
            
        # Realistic success probability curves:
        # Bank timeouts routed to healthy alternate switch succeed ~85%
        if root_cause == RootCause.BANK_TIMEOUT:
            success_prob = 0.85
        elif root_cause == RootCause.NETWORK_GLITCH:
            success_prob = 0.80
        elif root_cause == RootCause.INSUFFICIENT_FUNDS:
            # If auto-charged retry, lower success unless account topped up
            success_prob = 0.40
        else:
            success_prob = 0.50

        # High customer reliability adds a small positive factor
        if txn.customer_history.reliability_score > 0.90:
            success_prob += 0.05

        success = self.rng.random() < success_prob
        if success:
            notes = f"Payment successfully captured (INR {txn.amount}) via alternate gateway switch: {chosen_switch}."
        else:
            notes = f"Alternate switch {chosen_switch} also reported decline from issuer bank."

        return (success, chosen_switch, notes)

    def simulate_nudge_conversion(self, txn: Transaction, root_cause: RootCause) -> Tuple[bool, str]:
        """
        Simulates customer conversion when sent a personalized Hinglish WhatsApp nudge.
        Returns: (converted: bool, notes: str)
        """
        # Conversion probabilities based on human friction & root cause
        if root_cause == RootCause.WRONG_OTP:
            # Wrong OTP users are high-intent and readily complete with a 1-click OTP link
            conversion_prob = 0.72
        elif root_cause == RootCause.INSUFFICIENT_FUNDS:
            # User switches to alternate bank/UPI upon reminder
            conversion_prob = 0.62
        elif root_cause == RootCause.BANK_TIMEOUT:
            # User retries once notified that bank downtime is over
            conversion_prob = 0.68
        else:
            conversion_prob = 0.55

        # Personalization boost: high reliability customers convert higher
        if txn.customer_history.reliability_score > 0.85:
            conversion_prob += 0.08

        converted = self.rng.random() < conversion_prob
        if converted:
            notes = f"Customer opened WhatsApp nudge, clicked dynamic payment link, and completed payment of INR {txn.amount}."
        else:
            notes = "WhatsApp nudge delivered; customer did not complete authorization within the conversion window."

        return (converted, notes)

    def simulate_alt_method_conversion(self, txn: Transaction, target_method: str = "upi") -> Tuple[bool, str]:
        """
        Simulates customer conversion when offered an instant alternate payment method (e.g. UPI).
        """
        # UPI switch conversion in India is very high (~78%)
        conversion_prob = 0.78 if target_method == "upi" else 0.65
        converted = self.rng.random() < conversion_prob
        
        if converted:
            notes = f"Customer switched from failed card to {target_method.upper()} via Razorpay Custom Checkout and paid INR {txn.amount}."
        else:
            notes = f"Alternate method link sent ({target_method.upper()}); customer abandoned checkout session."

        return (converted, notes)

# Global Simulator singleton
razorpay_simulator = RazorpaySimulator()
