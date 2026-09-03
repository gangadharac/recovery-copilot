import os
import sys
import argparse
from pathlib import Path

# Force UTF-8 stdout encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import settings
from app.generator.synthetic_data import load_synthetic_transactions, generate_synthetic_transactions, save_synthetic_data
from app.pipeline.batch_orchestrator import batch_orchestrator

def print_banner(agent_mode=False):
    print("=" * 80)
    if agent_mode:
        print("  [REVENUE RECOVERY AGENT] - AUTONOMOUS AGENT BATCH")
        print("  Multi-Step ReAct Loop (Observe -> Diagnose -> Plan -> Guardrail -> Re-Plan)")
    else:
        print("  [REVENUE RECOVERY AGENT] - Event-Driven & Batch Recovery Engine")
        print("  Razorpay AI Buildathon (Track 03: AI Revenue Recovery)")
    print("=" * 80)

def print_metrics_summary(result):
    print("\n" + "-" * 80)
    print("BATCH RECOVERY PERFORMANCE HEADLINES")
    print("-" * 80)
    print(f"  * Total Failed Ingested:   {result.total_transactions} transactions")
    print(f"  * Total Revenue At Risk:   INR {result.total_at_risk:>12,.2f}")
    print(f"  * Total Revenue Recovered: INR {result.total_recovered:>12,.2f}")
    print(f"  * Overall Recovery Rate:   {result.recovery_rate_pct:>12.2f}%")
    print(f"  * Recovered vs Failed:     {result.recovered_count} recovered | {result.unrecovered_count} unrecovered")
    print(f"  * Human Ops Escalations:   {result.escalated_count}")
    print(f"  * Customer Nudges Sent:    {result.nudged_count}")
    print("-" * 80)

def print_agent_metrics_summary(result):
    print("\n" + "-" * 80)
    print("AUTONOMOUS AGENT PERFORMANCE & RE-PLANNING HEADLINES")
    print("-" * 80)
    print(f"  * Total Agent Runs:        {result.total_transactions}")
    print(f"  * Total Revenue At Risk:   INR {result.total_at_risk:>12,.2f}")
    print(f"  * Total Revenue Recovered: INR {result.total_recovered:>12,.2f}")
    print(f"  * Overall Recovery Rate:   {result.recovery_rate_pct:>12.2f}%")
    print(f"  * Recovered on 1st Action: {result.recovered_first_attempt_count} txns")
    print(f"  * Recovered via Re-Plan:   {result.recovered_after_replanning_count} txns")
    print(f"  * Escalated to Human Ops:  {result.escalated_count} txns")
    print(f"  * Abandoned / Give Up:     {result.unrecovered_count} txns")
    print(f"  * Average Iterations:      {result.avg_iterations:.2f} per txn")
    print(f"  * Max Iterations (3) Hit:  {result.max_iterations_reached_count} txns")
    print(f"  * Guardrail Interventions: {result.guardrail_interventions_count}")
    print("-" * 80)

def print_agent_tool_usage(tool_usage):
    print("\nTOOL USAGE BREAKDOWN (Autonomous Execution Counts)")
    print(f"{'Tool Name':<24} | {'Executions'}")
    print("-" * 40)
    for tool, count in tool_usage.items():
        print(f"{tool:<24} | {count:>10}")

def print_root_cause_breakdown(rc_breakdown):
    print("\nBREAKDOWN BY ROOT CAUSE")
    print(f"{'Root Cause':<22} | {'Count':<6} | {'INR At Risk':<14} | {'INR Recovered':<14} | {'Recovery Rate'}")
    print("-" * 80)
    for rc, stats in rc_breakdown.items():
        print(f"{rc:<22} | {stats['count']:<6} | {stats['amount_at_risk']:>14,.2f} | {stats['amount_recovered']:>14,.2f} | {stats['recovery_rate_pct']:>11.1f}%")

def print_action_breakdown(action_breakdown):
    print("\nBREAKDOWN BY RECOVERY ACTION")
    print(f"{'Action Decided':<22} | {'Count':<6} | {'INR Total':<14} | {'INR Recovered':<14}")
    print("-" * 80)
    for act, stats in action_breakdown.items():
        print(f"{act:<22} | {stats['count']:<6} | {stats['amount']:>14,.2f} | {stats['recovered_amount']:>14,.2f}")

def print_exception_list(exceptions, limit=6):
    print(f"\nHONEST EXCEPTION LIST (Showing {min(limit, len(exceptions))} of {len(exceptions)} unresolved cases)")
    print(f"{'Txn ID':<16} | {'Amount':<10} | {'Root Cause':<16} | {'Action':<15} | {'Unresolved Reason'}")
    print("-" * 80)
    for exc in exceptions[:limit]:
        print(f"{exc['transaction_id']:<16} | {exc['amount']:>10,.2f} | {exc['root_cause']:<16} | {exc['action_taken']:<15} | {exc['unresolved_reason']}")

def print_audit_sample(audit_trails, count=3):
    print(f"\nDETAILED AUDIT TRAIL SAMPLES ({count} representative end-to-end decisions)")
    print("-" * 80)
    for idx, record in enumerate(audit_trails[:count], 1):
        print(f"\n--- [Case {idx}: {record.transaction_id}] ---")
        print(f"  * Customer:       {record.customer_name} ({record.payment_method})")
        print(f"  * Amount:         INR {record.amount:,.2f}")
        print(f"  * Raw Error Code: {record.original_error_code}")
        print(f"  * Diagnosis:      {record.diagnosis.root_cause.value} ({record.diagnosis.source.value} - conf: {record.diagnosis.confidence:.2f})")
        print(f"    Reasoning:      {record.diagnosis.reasoning}")
        print(f"  * Strategy:       {record.strategy.action.value} (Original: {record.strategy.original_action.value})")
        print(f"    Guardrails:     Overridden={record.strategy.guardrail_overridden}")
        for g in record.strategy.guardrail_checks:
            status_str = "PASS" if g.passed else "OVERRIDDEN"
            print(f"      - [{status_str}] {g.rule_name}: {g.description}")
        print(f"  * Execution:      Status={record.execution.status} | Recovered={record.execution.recovered} (INR {record.execution.recovered_amount:,.2f})")
        if record.execution.nudge_message:
            print(f"  * WhatsApp Nudge: \"{record.execution.nudge_message}\"")
        if record.execution.gateway_switch_used:
            print(f"  * Gateway Switch: {record.execution.gateway_switch_used}")
        print(f"  * Notes:          {record.execution.execution_notes}")

def print_agent_trace_sample(agent_results, count=3):
    print(f"\nAUTONOMOUS AGENT RE-PLANNING SAMPLES ({count} representative execution traces)")
    print("-" * 80)
    for idx, res in enumerate(agent_results[:count], 1):
        print(f"\n--- [Agent Case {idx}: {res.transaction_id}] ---")
        print(f"  * Final Status:   {res.final_status.upper()} (Recovered: INR {res.recovered_amount:,.2f})")
        print(f"  * Total Iterations: {res.iterations_used}")
        print(f"  * Diagnosis:      {res.diagnosis.root_cause.value if res.diagnosis else 'unknown'}")
        print(f"  * Execution Trace:")
        for step in res.thought_history:
            print(f"    - Step {step.step_number}: [{step.selected_action.value}] -> {step.thought_summary}")
            print(f"      Feedback: {step.reflection_summary}")

def main():
    parser = argparse.ArgumentParser(description="Recovery Copilot - AI Revenue Recovery Agent")
    parser.add_argument("--agent", "-a", action="store_true", help="Run in Autonomous Agent Mode with Multi-Step Re-Planning")
    args = parser.parse_args()

    print_banner(agent_mode=args.agent)
    
    # 1. Load or generate synthetic data
    if not settings.SYNTHETIC_DATA_PATH.exists():
        print("Generating fresh synthetic dataset (125 realistic Razorpay failure records)...")
        txns = generate_synthetic_transactions(125)
        save_synthetic_data(txns)
    else:
        print(f"Loading synthetic transactions from {settings.SYNTHETIC_DATA_PATH}...")
        txns = load_synthetic_transactions()

    print(f"Loaded {len(txns)} transactions.")

    if args.agent:
        print("Executing Autonomous Recovery Agent Pipeline (Observe -> Diagnose -> Plan -> Guardrail -> Re-Plan)...")
        result = batch_orchestrator.process_batch_with_agent(txns)
        print_agent_metrics_summary(result)
        print_agent_tool_usage(result.tool_usage_breakdown)
        print_root_cause_breakdown(result.root_cause_breakdown)
        print_action_breakdown(result.action_breakdown)
        print_exception_list(result.exception_list, limit=6)
        print_agent_trace_sample(result.agent_results, count=3)
    else:
        print("Executing AI Recovery Pipeline (Diagnosis -> Strategy -> Guardrails -> Executor -> Audit)...")
        result = batch_orchestrator.process_batch(txns)
        print_metrics_summary(result)
        print_root_cause_breakdown(result.root_cause_breakdown)
        print_action_breakdown(result.action_breakdown)
        print_exception_list(result.exception_list, limit=6)
        print_audit_sample(result.audit_trails, count=3)

    print("\n" + "=" * 80)
    print(f"Audit trail stored in SQLite DB: {settings.DB_PATH}")
    print("To launch the interactive Pitch Dashboard:")
    print("    streamlit run app/reporting/dashboard.py")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()
