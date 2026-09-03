import uuid
import pytest
from app.generator.synthetic_data import generate_synthetic_transactions
from app.pipeline.batch_orchestrator import batch_orchestrator

def test_batch_pipeline_end_to_end_smoke():
    """Verifies end-to-end pipeline execution on a mini-batch of transactions."""
    txns = generate_synthetic_transactions(15, seed=99)
    run_id = f"smoke_test_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch(txns, run_id=run_id)
    assert result.total_transactions == 15
    assert result.total_at_risk > 0
    assert result.total_recovered >= 0
    assert 0.0 <= result.recovery_rate_pct <= 100.0
    assert len(result.audit_trails) == 15
    assert len(result.exception_list) == result.unrecovered_count
