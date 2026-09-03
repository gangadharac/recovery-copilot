import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # LLM Settings
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = "claude-3-5-haiku-20241022"
    
    # Hardcoded Guardrails (Enforced across Strategy Agent & Pipeline)
    MAX_RETRY_LIMIT: int = 3
    MIN_COOLDOWN_MINUTES: int = 30
    
    # Merchant Info
    MERCHANT_ID: str = "merch_acme_retail_in"
    MERCHANT_NAME: str = "Acme Retail India"
    
    # Razorpay Test Mode Configuration (Phase 4 & 6 Additive)
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    @property
    def is_razorpay_configured(self) -> bool:
        """Returns True if Razorpay API Key ID and Secret are provided."""
        return bool(self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET)

    @property
    def is_test_mode(self) -> bool:
        """
        Enforces test-mode safety: Only 'rzp_test_' keys are permitted.
        Returns True if empty (dev/mock) or starts with 'rzp_test_'.
        """
        if not self.RAZORPAY_KEY_ID:
            return True
        return self.RAZORPAY_KEY_ID.startswith("rzp_test_")

    def validate_test_mode_safety(self) -> None:
        """
        Explicit configuration guard: Prevents accidental use of live Razorpay credentials.
        Raises ValueError if production keys ('rzp_live_') are detected.
        """
        if self.RAZORPAY_KEY_ID.startswith("rzp_live_"):
            raise ValueError(
                "SECURITY GUARD TRIGGERED: Production Razorpay credentials ('rzp_live_...') detected! "
                "Revenue Recovery Agent is strictly bounded to Razorpay Test Mode ('rzp_test_...')."
            )
    
    # Storage & Data Paths
    BASE_DIR: Path = BASE_DIR
    DATA_DIR: Path = BASE_DIR / "data"
    DB_PATH: Path = BASE_DIR / "data" / "recovery_audit.db"
    SYNTHETIC_DATA_PATH: Path = BASE_DIR / "data" / "synthetic_transactions.json"
    SYNTHETIC_CSV_PATH: Path = BASE_DIR / "data" / "synthetic_transactions.csv"
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

# Ensure data directory exists
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
