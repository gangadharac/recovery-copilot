import logging
from typing import Optional, Tuple
from requests.auth import HTTPBasicAuth
from app.config import settings

logger = logging.getLogger(__name__)

class RazorpayClient:
    """
    Secure Razorpay API Client bounded strictly to Test Mode.
    Provides authentication, base URL resolution, and test-mode safety validation.
    Guarantees secrets are never printed, logged, or leaked in responses.
    """
    BASE_URL = "https://api.razorpay.com/v1"

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None
    ):
        self._key_id = key_id if key_id is not None else settings.RAZORPAY_KEY_ID
        self._key_secret = key_secret if key_secret is not None else settings.RAZORPAY_KEY_SECRET

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def is_configured(self) -> bool:
        """Returns True if both API key ID and secret are configured."""
        return bool(self._key_id and self._key_secret)

    def validate_safety(self) -> None:
        """
        Validates credentials to prevent accidental live-money / production usage.
        Raises ValueError if production credentials ('rzp_live_') are configured.
        """
        if self._key_id and self._key_id.startswith("rzp_live_"):
            raise ValueError(
                "SECURITY GUARD TRIGGERED: Production Razorpay credentials ('rzp_live_...') detected! "
                "Revenue Recovery Agent is strictly bounded to Razorpay Test Mode ('rzp_test_...')."
            )

    def get_auth(self) -> Optional[HTTPBasicAuth]:
        """
        Returns HTTPBasicAuth for requests if configured.
        Raises ValueError if live credentials are used.
        """
        if not self.is_configured:
            return None
        self.validate_safety()
        return HTTPBasicAuth(self._key_id, self._key_secret)

    def get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "User-Agent": "RevenueRecoveryAgent/1.0"
        }

# Global singleton
razorpay_client = RazorpayClient()
