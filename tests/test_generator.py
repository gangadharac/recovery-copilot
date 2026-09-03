import pytest
from app.generator.synthetic_data import generate_synthetic_transactions
from app.schemas.transaction import Transaction

def test_generator_smoke():
    """Verifies synthetic data generator produces valid transactions matching schema."""
    txns = generate_synthetic_transactions(20)
    assert len(txns) == 20
    for txn in txns:
        assert isinstance(txn, Transaction)
        assert txn.amount > 0
        assert txn.currency == "INR"
        assert txn.error_code is not None
        assert txn.customer_name is not None
