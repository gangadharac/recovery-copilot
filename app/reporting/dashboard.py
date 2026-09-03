import warnings
warnings.filterwarnings("ignore")

import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import AuditLogModel, BatchRunModel, TransactionModel, AgentTraceModel, WebhookEventModel
from app.generator.synthetic_data import load_synthetic_transactions, generate_synthetic_transactions, save_synthetic_data
from app.pipeline.batch_orchestrator import batch_orchestrator
from app.schemas.transaction import Transaction, PaymentMethod, PaymentMethodDetails, CustomerHistory
from app.agents.diagnosis_agent import diagnosis_agent
from app.agents.strategy_agent import strategy_agent
from app.executor.recovery_executor import recovery_executor
from app.agents.recovery_agent import recovery_agent

# Initialize database schema and migrations
init_db()

# Page Configuration
st.set_page_config(
    page_title="Revenue Recovery Agent | Razorpay AI Revenue Recovery",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for polished Razorpay-style UI
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #0c2340;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.05rem;
        color: #506690;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1.2rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .guardrail-badge-pass {
        background-color: #dcfce7;
        color: #15803d;
        padding: 4px 8px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .guardrail-badge-override {
        background-color: #fee2e2;
        color: #b91c1c;
        padding: 4px 8px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .nudge-box {
        background-color: #e0f2fe;
        border-left: 4px solid #0284c7;
        padding: 12px;
        border-radius: 4px;
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    }
    .step-card {
        background-color: #f8fafc;
        border-left: 4px solid #3b82f6;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .step-card-success {
        background-color: #f0fdf4;
        border-left: 4px solid #22c55e;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .step-card-fail {
        background-color: #fef2f2;
        border-left: 4px solid #ef4444;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# Helper functions to load data from SQLite
@st.cache_data(ttl=30)
def load_db_data():
    db: Session = SessionLocal()
    try:
        audit_query = db.query(AuditLogModel).all()
        runs_query = db.query(BatchRunModel).order_by(BatchRunModel.timestamp.desc()).all()
        traces_query = db.query(AgentTraceModel).order_by(AgentTraceModel.created_at.asc()).all()
        wh_query = db.query(WebhookEventModel).order_by(WebhookEventModel.received_at.desc()).all()
        
        if not audit_query:
            return None, None, None, None
            
        audit_data = []
        for a in audit_query:
            audit_data.append({
                "audit_id": a.audit_id,
                "batch_run_id": a.batch_run_id,
                "transaction_id": a.transaction_id,
                "customer_name": a.customer_name,
                "customer_id": a.customer_id,
                "amount": a.amount,
                "currency": a.currency,
                "payment_method": a.payment_method,
                "original_error_code": a.original_error_code,
                "root_cause": a.root_cause,
                "diagnosis_confidence": a.diagnosis_confidence,
                "diagnosis_source": a.diagnosis_source,
                "diagnosis_reasoning": a.diagnosis_reasoning,
                "recommended_action": a.recommended_action,
                "original_action": a.original_action,
                "guardrail_overridden": a.guardrail_overridden,
                "guardrail_checks": a.guardrail_checks,
                "strategy_reasoning": a.strategy_reasoning,
                "give_up_reason": a.give_up_reason,
                "execution_status": a.execution_status,
                "recovered": a.recovered,
                "recovered_amount": a.recovered_amount,
                "gateway_switch_used": a.gateway_switch_used,
                "nudge_channel": a.nudge_channel,
                "nudge_message": a.nudge_message,
                "unresolved_reason": a.unresolved_reason,
                "execution_notes": a.execution_notes,
                "agent_run_id": getattr(a, "agent_run_id", None),
                "iterations_used": getattr(a, "iterations_used", 1),
                "audit_summary": a.audit_summary,
                "created_at": a.created_at
            })
        df_audit = pd.DataFrame(audit_data)
        
        runs_data = []
        for r in runs_query:
            runs_data.append({
                "run_id": r.run_id,
                "timestamp": r.timestamp,
                "total_transactions": r.total_transactions,
                "total_amount_at_risk": r.total_amount_at_risk,
                "total_amount_recovered": r.total_amount_recovered,
                "recovery_rate_pct": r.recovery_rate_pct,
                "recovered_count": r.recovered_count,
                "unrecovered_count": r.unrecovered_count,
                "escalated_count": r.escalated_count,
                "nudged_count": r.nudged_count,
                "root_cause_breakdown": r.root_cause_breakdown,
                "action_breakdown": r.action_breakdown
            })
        df_runs = pd.DataFrame(runs_data)

        traces_data = []
        for t in traces_query:
            traces_data.append({
                "trace_id": t.trace_id,
                "batch_run_id": t.batch_run_id,
                "transaction_id": t.transaction_id,
                "agent_run_id": t.agent_run_id,
                "iteration_number": t.iteration_number,
                "observation": t.observation,
                "thought_summary": t.thought_summary,
                "action_tool": t.action_tool,
                "tool_input_summary": t.tool_input_summary,
                "tool_result_status": t.tool_result_status,
                "verification_status": t.verification_status,
                "guardrail_decision": t.guardrail_decision,
                "recovered_amount": t.recovered_amount,
                "final_status": t.final_status,
                "termination_reason": t.termination_reason,
                "created_at": t.created_at
            })
        df_traces = pd.DataFrame(traces_data) if traces_data else pd.DataFrame()

        wh_data = []
        for w in wh_query:
            wh_data.append({
                "id": w.id,
                "event_id": w.event_id,
                "event_type": w.event_type,
                "payment_id": w.payment_id,
                "order_id": w.order_id,
                "status": w.status,
                "received_at": w.received_at,
                "payload_hash": w.payload_hash
            })
        df_webhooks = pd.DataFrame(wh_data) if wh_data else pd.DataFrame()

        # Phase 6 Additive: Recovery Attempts & Payment Links
        from app.db.models import RecoveryAttemptModel
        att_query = db.query(RecoveryAttemptModel).order_by(RecoveryAttemptModel.created_at.desc()).all()
        att_data = []
        for a in att_query:
            att_data.append({
                "id": a.id,
                "transaction_id": a.transaction_id,
                "agent_run_id": a.agent_run_id,
                "payment_link_id": a.payment_link_id,
                "payment_link_url": a.payment_link_url,
                "status": a.status,
                "amount": a.amount,
                "currency": a.currency,
                "payment_id": getattr(a, "payment_id", None),
                "verification_source": getattr(a, "verification_source", None),
                "customer_contact": a.customer_contact,
                "customer_email": a.customer_email,
                "created_at": a.created_at,
                "recovered_at": a.recovered_at
            })
        df_attempts = pd.DataFrame(att_data) if att_data else pd.DataFrame()

        return df_audit, df_runs, df_traces, df_webhooks, df_attempts
    finally:
        db.close()

# Sidebar Controls
st.sidebar.image("https://razorpay.com/assets/razorpay-glyph.svg", width=50)
st.sidebar.title("Revenue Recovery Agent")
st.sidebar.caption("Razorpay AI Buildathon | Track 03: AI Revenue Recovery")
st.sidebar.markdown("---")

st.sidebar.subheader("🕹️ Batch Processing")
col_b1, col_b2 = st.sidebar.columns(2)

with col_b1:
    if st.button("🔄 Original Pipeline", type="secondary", use_container_width=True, help="Run single-pass linear recovery pipeline"):
        with st.spinner("Processing batch through Diagnosis -> Strategy -> Guardrails -> Execution..."):
            txns = generate_synthetic_transactions(125, seed=int(datetime.now(timezone.utc).timestamp()) % 1000)
            batch_orchestrator.process_batch(txns)
            st.cache_data.clear()
            st.rerun()

with col_b2:
    if st.button("🤖 Autonomous Agent", type="primary", use_container_width=True, help="Run multi-step autonomous agent loop with re-planning"):
        with st.spinner("Executing Autonomous Recovery Agent loop (Observe -> Diagnose -> Plan -> Guardrail -> Re-Plan)..."):
            txns = generate_synthetic_transactions(125, seed=int(datetime.now(timezone.utc).timestamp()) % 1000)
            batch_orchestrator.process_batch_with_agent(txns)
            st.cache_data.clear()
            st.rerun()

# Load Data from Database
df_audit, df_runs, df_traces, df_webhooks, df_attempts = load_db_data()

if df_audit is None or df_audit.empty:
    st.info("No transaction data found in SQLite. Initializing default 125-transaction batch run...")
    txns = generate_synthetic_transactions(125, seed=42)
    save_synthetic_data(txns)
    batch_orchestrator.process_batch(txns)
    st.cache_data.clear()
    df_audit, df_runs, df_traces, df_webhooks, df_attempts = load_db_data()

# Batch Run Selector
available_runs = df_runs["run_id"].tolist() if df_runs is not None and not df_runs.empty else ["default"]
selected_run_id = st.sidebar.selectbox("Select Batch Run ID:", available_runs, index=0)

# Filter Data for Selected Batch Run
df_selected = df_audit[df_audit["batch_run_id"] == selected_run_id] if "batch_run_id" in df_audit.columns else df_audit
if df_selected.empty:
    df_selected = df_audit

# Header Section
st.markdown("<div class='main-header'>⚡ Revenue Recovery Agent</div>", unsafe_allow_html=True)
st.markdown("<div class='sub-header'>Autonomous, Guardrailed Payment Recovery Engine for Indian Merchants | Measured ₹ Recovered with Compliant Safety Overrides</div>", unsafe_allow_html=True)

# Calculate Top-level Metrics (Phase 6 Revenue Accounting)
total_txns = len(df_selected)
total_at_risk = df_selected["amount"].sum()
total_recovered = df_selected["recovered_amount"].sum()
recovery_pending = df_selected[df_selected["execution_status"] == "recovery_pending"]["amount"].sum() if "execution_status" in df_selected.columns else 0.0
pending_count = (df_selected["execution_status"] == "recovery_pending").sum() if "execution_status" in df_selected.columns else 0
recovery_rate = (total_recovered / total_at_risk * 100) if total_at_risk > 0 else 0
recovered_count = df_selected["recovered"].sum()
unrecovered_count = total_txns - recovered_count - pending_count
escalated_count = (df_selected["recommended_action"] == "escalate_human").sum()
nudged_count = df_selected["recommended_action"].isin(["nudge_customer", "offer_alt_method", "whatsapp_nudge", "upi_switch", "payment_link"]).sum()
guardrail_overrides_count = df_selected["guardrail_overridden"].sum()

# Top KPI Metric Cards
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
with kpi1:
    st.metric("Revenue At Risk", f"₹{total_at_risk:,.2f}", f"{total_txns} txns")
with kpi2:
    st.metric("Recovery Pending", f"₹{recovery_pending:,.2f}", f"{pending_count} payment links")
with kpi3:
    st.metric("Revenue Recovered", f"₹{total_recovered:,.2f}", f"{recovered_count} verified")
with kpi4:
    st.metric("Recovery Rate", f"{recovery_rate:.1f}%", f"+₹{total_recovered:,.0f}")
with kpi5:
    st.metric("Guardrail Overrides", f"{guardrail_overrides_count}", "Compliant interventions")

st.markdown("---")

# Main Tabs
tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "📊 Performance Breakdown",
    "🛡️ Guardrails & Compliance",
    "📋 Honest Exception List",
    "🔬 Audit Trail Inspector",
    "⚡ Live Scenario Tester",
    "🤖 Agent Monitor (Phase 3)",
    "📡 Live Webhook Events (Phase 5)"
])

# ----------------- TAB 1: PERFORMANCE BREAKDOWN -----------------
with tab1:
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        st.subheader("₹ Revenue at Risk vs ₹ Recovered by Root Cause")
        rc_summary = df_selected.groupby("root_cause").agg(
            at_risk=("amount", "sum"),
            recovered=("recovered_amount", "sum"),
            count=("transaction_id", "count")
        ).reset_index()
        rc_summary["recovery_rate_pct"] = (rc_summary["recovered"] / rc_summary["at_risk"] * 100).round(1)
        
        fig_rc = go.Figure(data=[
            go.Bar(name='₹ at Risk', x=rc_summary['root_cause'], y=rc_summary['at_risk'], marker_color='#94a3b8'),
            go.Bar(name='₹ Recovered', x=rc_summary['root_cause'], y=rc_summary['recovered'], marker_color='#10b981')
        ])
        fig_rc.update_layout(barmode='group', height=360, margin=dict(l=20, r=20, t=30, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig_rc, use_container_width=True)

    with col_chart2:
        st.subheader("Distribution of Decided Recovery Actions")
        action_counts = df_selected["recommended_action"].value_counts().reset_index()
        action_counts.columns = ["Action", "Count"]
        
        fig_act = px.pie(
            action_counts,
            values="Count",
            names="Action",
            hole=0.45,
            color_discrete_sequence=px.colors.qualitative.Safe
        )
        fig_act.update_layout(height=360, margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig_act, use_container_width=True)

    st.markdown("### 📋 Measured Recovery Rates by Category")
    st.dataframe(
        rc_summary[["root_cause", "count", "at_risk", "recovered", "recovery_rate_pct"]].rename(
            columns={
                "root_cause": "Diagnostic Root Cause",
                "count": "Total Transactions",
                "at_risk": "Total ₹ at Risk",
                "recovered": "Total ₹ Recovered",
                "recovery_rate_pct": "Recovery Rate (%)"
            }
        ).style.format({
            "Total ₹ at Risk": "₹{:,.2f}",
            "Total ₹ Recovered": "₹{:,.2f}",
            "Recovery Rate (%)": "{:.1f}%"
        }),
        use_container_width=True
    )

# ----------------- TAB 2: GUARDRAILS & COMPLIANCE -----------------
with tab2:
    st.subheader("🛡️ Hardcoded Safety Guardrail Interventions")
    st.markdown("""
    Every autonomous recovery action passes through **4 non-negotiable Python guardrails** before execution.
    These safety rules guarantee zero-hallucination compliance across retry limits, customer consent, and risk isolation.
    """)
    
    g_col1, g_col2, g_col3, g_col4 = st.columns(4)
    with g_col1:
        st.info("**1. Risk Safety Isolation**\n\n`risk_blocked` failures are 100% quarantined to human ops. Auto-recovery strictly prohibited (0% recovery by design).")
    with g_col2:
        st.info("**2. Hard Max Retry Cap**\n\nHard stop at 3 attempts. Prevents duplicate charges and stops aggressive retrying on exhausted cards.")
    with g_col3:
        st.info("**3. Consent Enforcement**\n\nSilent auto-debit prohibited without explicit customer consent. Re-routes to interactive WhatsApp nudge.")
    with g_col4:
        st.info("**4. 30-Min Cooldown Window**\n\nEnforces banking cooldown periods between attempts to avoid issuer throttling and gateway bans.")

    st.markdown("### 🔍 Guardrail Intervention Audit Log")
    overridden_df = df_selected[df_selected["guardrail_overridden"] == True]
    if not overridden_df.empty:
        st.dataframe(
            overridden_df[[
                "transaction_id", "customer_name", "amount", "root_cause", "original_action", "recommended_action", "strategy_reasoning"
            ]].rename(columns={
                "transaction_id": "Txn ID",
                "customer_name": "Customer",
                "amount": "Amount (₹)",
                "root_cause": "Root Cause",
                "original_action": "Original Proposal",
                "recommended_action": "Compliant Action",
                "strategy_reasoning": "Guardrail Rationale"
            }).style.format({"Amount (₹)": "₹{:,.2f}"}),
            use_container_width=True
        )
    else:
        st.success("All transactions in this batch executed strictly within safety parameters without requiring manual overrides.")

# ----------------- TAB 3: HONEST EXCEPTION LIST -----------------
with tab3:
    st.subheader("📋 Honest Exception List (Unrecovered Cases)")
    st.markdown("""
    Per Razorpay AI Buildathon criteria, we transparently detail every single unrecovered transaction with an explicit, human-readable reason.
    No cherry-picked data or hidden losses.
    """)
    
    unrec_df = df_selected[df_selected["recovered"] == False]
    st.write(f"Total Unresolved Transactions: **{len(unrec_df)}** | Unrecovered Capital at Risk: **₹{unrec_df['amount'].sum():,.2f}**")
    
    col_filter1, col_filter2 = st.columns(2)
    with col_filter1:
        filter_rc = st.multiselect("Filter by Root Cause", df_selected["root_cause"].unique().tolist(), default=df_selected["root_cause"].unique().tolist())
    with col_filter2:
        filter_act = st.multiselect("Filter by Action Taken", df_selected["recommended_action"].unique().tolist(), default=df_selected["recommended_action"].unique().tolist())
        
    filtered_unrec = unrec_df[unrec_df["root_cause"].isin(filter_rc) & unrec_df["recommended_action"].isin(filter_act)]
    
    st.dataframe(
        filtered_unrec[[
            "transaction_id", "customer_name", "amount", "payment_method", "root_cause", "recommended_action", "unresolved_reason"
        ]].rename(columns={
            "transaction_id": "Txn ID",
            "customer_name": "Customer",
            "amount": "Amount (₹)",
            "payment_method": "Method",
            "root_cause": "Root Cause",
            "recommended_action": "Action",
            "unresolved_reason": "Honest Unresolved Reason"
        }).style.format({"Amount (₹)": "₹{:,.2f}"}),
        use_container_width=True
    )
    
    csv_data = filtered_unrec.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Export Honest Exception List (CSV)",
        data=csv_data,
        file_name=f"honest_exceptions_{selected_run_id}.csv",
        mime="text/csv"
    )

# ----------------- TAB 4: AUDIT TRAIL INSPECTOR -----------------
with tab4:
    st.subheader("🔬 End-to-End Audit Trail Inspector")
    st.markdown("Inspect the full diagnostic reasoning, guardrail checklist, and execution notes for any specific transaction.")
    
    txn_options = df_selected["transaction_id"].tolist()
    selected_txn_id = st.selectbox("Select Transaction to Inspect:", txn_options)
    
    txn_row = df_selected[df_selected["transaction_id"] == selected_txn_id].iloc[0]
    
    audit_c1, audit_c2, audit_c3 = st.columns(3)
    with audit_c1:
        st.markdown(f"**Customer:** {txn_row['customer_name']}")
        st.markdown(f"**Amount:** ₹{txn_row['amount']:,.2f} ({txn_row['currency']})")
        st.markdown(f"**Payment Method:** `{txn_row['payment_method']}`")
        st.markdown(f"**Original Error Code:** `{txn_row['original_error_code']}`")
    with audit_c2:
        st.markdown(f"**Diagnosed Root Cause:** `{txn_row['root_cause']}`")
        st.markdown(f"**Confidence:** {txn_row['diagnosis_confidence']:.2f} ({txn_row['diagnosis_source']})")
        st.markdown(f"**Recommended Action:** `{txn_row['recommended_action']}`")
        overridden_badge = "<span class='guardrail-badge-override'>OVERRIDDEN</span>" if txn_row['guardrail_overridden'] else "<span class='guardrail-badge-pass'>PASSED</span>"
        st.markdown(f"**Guardrail Evaluation:** {overridden_badge}", unsafe_allow_html=True)
    with audit_c3:
        status_color = "green" if txn_row['recovered'] else "red"
        st.markdown(f"**Outcome Status:** `{txn_row['execution_status']}`")
        st.markdown(f"**Recovered:** :{status_color}[{'YES (₹' + f'{txn_row.recovered_amount:,.2f}' + ')' if txn_row['recovered'] else 'NO (₹0.00)'}]")
        st.markdown(f"**Iterations:** {txn_row.get('iterations_used', 1)}")

    st.markdown("---")
    st.markdown("#### 🧠 Diagnostic & Strategy Reasoning")
    st.info(f"**Diagnosis Reasoning:** {txn_row['diagnosis_reasoning']}")
    st.write(f"**Strategy Decision:** {txn_row['strategy_reasoning']}")
    
    if txn_row["nudge_message"]:
        st.markdown("#### 📱 Personalized Hinglish WhatsApp Nudge Preview")
        st.markdown(f"<div class='nudge-box'>💬 <b>WhatsApp Business API Payload</b><br><br><i>\"{txn_row['nudge_message']}\"</i></div>", unsafe_allow_html=True)
        st.write("")
    if txn_row["gateway_switch_used"]:
        st.write(f"**Alternate Gateway Switch Used:** `{txn_row['gateway_switch_used']}`")
    st.write(f"**Execution Notes:** {txn_row['execution_notes']}")

# ----------------- TAB 5: LIVE SCENARIO TESTER -----------------
with tab5:
    st.subheader("⚡ Live Single-Transaction Recovery Sandbox")
    st.markdown("Simulate any arbitrary failed transaction scenario live and watch the autonomous agent execute in real-time.")

    sim_mode = st.radio(
        "Select Engine Mode:",
        ["Autonomous Recovery Agent (Multi-Step Re-Plan)", "Original Pipeline (Single-Pass)"],
        horizontal=True
    )
    
    sim_col1, sim_col2 = st.columns(2)
    with sim_col1:
        sim_name = st.text_input("Customer Name", "Arnav Malhotra")
        sim_amount = st.number_input("Transaction Amount (INR)", min_value=100.0, max_value=200000.0, value=3499.0, step=100.0)
        sim_method = st.selectbox("Payment Method", [PaymentMethod.CARD, PaymentMethod.UPI, PaymentMethod.NETBANKING])
        sim_error = st.selectbox("Simulated Error Code", [
            "GATEWAY_ERROR",
            "insufficient_funds",
            "otp_timeout",
            "card_expired",
            "risk_blocked",
            "network_error",
            "UNKNOWN_SWITCH_ERROR"
        ])
    with sim_col2:
        sim_retries = st.slider("Past Retry Count", 0, 4, 0)
        sim_consent = st.checkbox("Customer Auto-Charge Consent Enabled", value=False)
        sim_reliability = st.slider("Customer Reliability Score", 0.1, 1.0, 0.90)
        sim_error_desc = st.text_input("Error Description", "Bank switch timed out while negotiating payment session")

    if st.button("🚀 Run Live Recovery Simulation", type="primary"):
        sim_txn = Transaction(
            transaction_id=f"txn_live_{int(datetime.now(timezone.utc).timestamp())}",
            customer_id="cust_live_test",
            customer_name=sim_name,
            customer_phone="+919988776655",
            customer_email="arnav.m@example.com",
            amount=sim_amount,
            currency="INR",
            payment_method=sim_method,
            payment_method_details=PaymentMethodDetails(
                card_network="Visa" if sim_method == PaymentMethod.CARD else None,
                bank_code="HDFC",
                upi_vpa=f"{sim_name.lower().replace(' ', '')}@okhdfcbank" if sim_method == PaymentMethod.UPI else None
            ),
            error_code=sim_error,
            error_description=sim_error_desc,
            error_source="gateway" if "UNKNOWN" in sim_error else ("risk_engine" if sim_error == "risk_blocked" else "bank_switch"),
            retry_count=sim_retries,
            last_retry_timestamp=None,
            timestamp=datetime.now(timezone.utc),
            auto_charge_consent=sim_consent,
            customer_history=CustomerHistory(
                lifetime_successful_transactions=int(sim_reliability * 20),
                lifetime_failed_transactions=2,
                reliability_score=sim_reliability
            )
        )
        
        st.markdown("---")
        st.markdown("### 🎯 Live Simulation Results")

        if sim_mode == "Autonomous Recovery Agent (Multi-Step Re-Plan)":
            agent_result = recovery_agent.recover(sim_txn)
            
            res1, res2, res3 = st.columns(3)
            with res1:
                st.metric("Diagnosed Cause", f"{agent_result.diagnosis.root_cause.value if agent_result.diagnosis else 'unknown'}")
            with res2:
                st.metric("Total Iterations", f"{agent_result.iterations_used} / {agent_result.state.max_iterations}")
            with res3:
                rec_status = "✅ RECOVERED" if agent_result.final_status == "recovered" else f"❌ {agent_result.final_status.upper()}"
                st.metric("Final Status", rec_status, f"₹{agent_result.recovered_amount:,.2f}")

            st.markdown("#### 🔄 Step-by-Step Autonomous Execution Timeline")
            for step in agent_result.thought_history:
                card_class = "step-card-success" if step.verification_result == "SUCCESS" else ("step-card-fail" if step.verification_result == "FAILED" else "step-card")
                st.markdown(f"""
                <div class="{card_class}">
                    <b>Iteration {step.step_number}: Selected Tool <code>{step.selected_action.value}</code></b><br>
                    <p style='margin: 6px 0 2px 0;'><b>Thought:</b> {step.thought_summary}</p>
                    <p style='margin: 0; color: #475569;'><b>Verification:</b> {step.reflection_summary}</p>
                </div>
                """, unsafe_allow_html=True)

        else:
            # Original Single-Pass Pipeline
            d_res = diagnosis_agent.diagnose(sim_txn)
            s_res = strategy_agent.decide_action(sim_txn, d_res)
            e_res = recovery_executor.execute(sim_txn, d_res, s_res)
            
            res1, res2, res3 = st.columns(3)
            with res1:
                st.metric("Diagnosed Cause", f"{d_res.root_cause.value} ({d_res.source.value})")
            with res2:
                st.metric("Decided Action", s_res.action.value)
            with res3:
                rec_status = "✅ RECOVERED" if e_res.recovered else "❌ UNRESOLVED"
                st.metric("Recovery Outcome", rec_status, f"₹{e_res.recovered_amount:,.2f}")
                
            if s_res.guardrail_overridden:
                st.warning(f"**Guardrail Intervened:** {s_res.reasoning}")
            else:
                st.success(f"**Strategy Reasoning:** {s_res.reasoning}")
                
            if e_res.nudge_message:
                st.markdown(f"<div class='nudge-box'>📱 <b>Generated WhatsApp Nudge</b><br><br>{e_res.nudge_message}</div>", unsafe_allow_html=True)

# ----------------- TAB 6: AGENT MONITOR (PHASE 3) -----------------
with tab6:
    st.subheader("🤖 Autonomous Agent Execution Monitor")
    st.markdown("Monitor multi-step autonomous agent runs, tool usage frequency, re-planning statistics, and full step-by-step traces.")

    if df_traces is not None and not df_traces.empty and "batch_run_id" in df_traces.columns:
        selected_traces = df_traces[df_traces["batch_run_id"] == selected_run_id]
        if selected_traces.empty:
            selected_traces = df_traces
    else:
        selected_traces = pd.DataFrame()

    # Top KPI Metrics for Agent
    agent_col1, agent_col2, agent_col3, agent_col4 = st.columns(4)
    with agent_col1:
        st.metric("Agent Runs", f"{total_txns}", "Batch size")
    with agent_col2:
        multi_step_count = (df_selected["iterations_used"] > 1).sum() if "iterations_used" in df_selected.columns else 0
        st.metric("Re-Planned Txns", f"{multi_step_count}", f"{(multi_step_count/total_txns*100):.1f}% adapted" if total_txns > 0 else "0%")
    with agent_col3:
        avg_iters = df_selected["iterations_used"].mean() if "iterations_used" in df_selected.columns else 1.0
        st.metric("Average Iterations", f"{avg_iters:.2f}", "Max cap: 3")
    with agent_col4:
        first_attempt_rec = ((df_selected["recovered"] == True) & (df_selected["iterations_used"] == 1)).sum() if "iterations_used" in df_selected.columns else recovered_count
        replan_rec = ((df_selected["recovered"] == True) & (df_selected["iterations_used"] > 1)).sum() if "iterations_used" in df_selected.columns else 0
        st.metric("Re-Plan Recovery", f"{replan_rec} saved", f"{first_attempt_rec} on 1st try")

    st.markdown("---")

    # Tool Usage Breakdown
    st.markdown("### 🛠️ Autonomous Tool Usage Breakdown")
    tool_col1, tool_col2 = st.columns(2)
    with tool_col1:
        if not selected_traces.empty:
            tool_counts = selected_traces["action_tool"].value_counts().reset_index()
            tool_counts.columns = ["Tool Name", "Executions"]
            fig_tools = px.bar(
                tool_counts,
                x="Tool Name",
                y="Executions",
                color="Tool Name",
                color_discrete_sequence=px.colors.qualitative.Bold,
                title="Total Tool Invocations Across All Iterations"
            )
            fig_tools.update_layout(height=340, showlegend=False)
            st.plotly_chart(fig_tools, use_container_width=True)
        else:
            st.info("Run an Autonomous Agent batch to populate tool execution metrics.")

    with tool_col2:
        st.markdown("#### 🔄 Re-Planning Efficiency")
        replan_data = pd.DataFrame([
            {"Outcome": "1st Attempt Recovered", "Count": first_attempt_rec},
            {"Outcome": "Re-Planned Recovered", "Count": replan_rec},
            {"Outcome": "Escalated to Ops", "Count": escalated_count},
            {"Outcome": "Abandoned / Give Up", "Count": unrecovered_count - escalated_count}
        ])
        fig_replan = px.pie(
            replan_data,
            values="Count",
            names="Outcome",
            hole=0.45,
            color_discrete_sequence=["#10b981", "#3b82f6", "#f59e0b", "#ef4444"]
        )
        fig_replan.update_layout(height=340, margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig_replan, use_container_width=True)

    st.markdown("---")

    # Step-by-Step Execution Trace Explorer
    st.markdown("### 🔍 Transaction Multi-Step Trace Explorer")
    trace_txn_id = st.selectbox(
        "Select Transaction to Inspect Agent Trace:",
        df_selected["transaction_id"].tolist(),
        key="trace_select"
    )

    if not selected_traces.empty and trace_txn_id in selected_traces["transaction_id"].values:
        txn_traces = selected_traces[selected_traces["transaction_id"] == trace_txn_id].sort_values("iteration_number")
        
        st.write(f"**Transaction ID:** `{trace_txn_id}` | Total Iterations: **{len(txn_traces)}**")
        for _, tr in txn_traces.iterrows():
            card_class = "step-card-success" if tr['verification_status'] == "SUCCESS" else ("step-card-fail" if tr['verification_status'] == "FAILED" else "step-card")
            st.markdown(f"""
            <div class="{card_class}">
                <b>Iteration {tr['iteration_number']}: Executed <code>{tr['action_tool']}</code></b> 
                <span style='float: right;'><b>Status:</b> {tr['verification_status']}</span><br>
                <p style='margin: 6px 0 2px 0;'><b>Thought / Reason:</b> {tr['thought_summary']}</p>
                <p style='margin: 0; color: #475569;'><b>Observation:</b> {tr['observation']}</p>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("Run `python run_batch.py --agent` or click '🤖 Autonomous Agent' in the sidebar to populate multi-step traces for this transaction.")

# ----------------- TAB 7: LIVE WEBHOOK EVENTS (PHASE 5) -----------------
with tab7:
    st.subheader("📡 Live Webhook Events & Real-Time Agent Recovery")
    st.markdown("""
    **Razorpay Test Mode / Webhook-Triggered Recovery Activity**  
    Ingests real `payment.failed` webhook events from Razorpay, runs HMAC-SHA256 signature verification and idempotency checks, and executes the autonomous **RecoveryAgent** loop with full audit persistence.
    """)

    wh_col1, wh_col2, wh_col3, wh_col4 = st.columns(4)
    with wh_col1:
        total_wh = len(df_webhooks) if df_webhooks is not None else 0
        st.metric("Total Webhook Events", f"{total_wh}", "Ingested & Verified")
    with wh_col2:
        fail_wh = (df_webhooks["event_type"] == "payment.failed").sum() if df_webhooks is not None and not df_webhooks.empty else 0
        st.metric("Payment Failures", f"{fail_wh}", "Triggered RecoveryAgent")
    with wh_col3:
        cap_wh = (df_webhooks["event_type"].isin(["payment.captured", "order.paid"])).sum() if df_webhooks is not None and not df_webhooks.empty else 0
        st.metric("Success Events", f"{cap_wh}", "Verified Lifecycle")
    with wh_col4:
        dup_wh = (df_webhooks["status"] == "duplicate_ignored").sum() if df_webhooks is not None and not df_webhooks.empty else 0
        st.metric("Duplicate Blocks", f"{dup_wh}", "Idempotency Protected")

    st.markdown("---")

    if df_webhooks is not None and not df_webhooks.empty:
        st.markdown("### 📥 Ingested Razorpay Webhook Event Stream")
        st.dataframe(
            df_webhooks[["event_id", "event_type", "payment_id", "order_id", "status", "received_at"]].rename(
                columns={
                    "event_id": "Event ID",
                    "event_type": "Event Type",
                    "payment_id": "Payment ID",
                    "order_id": "Order ID",
                    "status": "Processing Status",
                    "received_at": "Received At"
                }
            ),
            use_container_width=True
        )

        st.markdown("### 🤖 Correlated Autonomous Agent Recovery Outcomes")
        # Filter audit logs that were triggered via webhooks
        wh_audits = df_audit[df_audit["batch_run_id"].str.startswith("webhook_", na=False)] if "batch_run_id" in df_audit.columns else pd.DataFrame()
        if not wh_audits.empty:
            st.dataframe(
                wh_audits[[
                    "transaction_id", "batch_run_id", "agent_run_id", "amount", "root_cause", "recommended_action", "iterations_used", "execution_status", "recovered", "recovered_amount"
                ]].rename(
                    columns={
                        "transaction_id": "Payment / Txn ID",
                        "batch_run_id": "Webhook Run ID",
                        "agent_run_id": "Agent Run ID",
                        "amount": "Amount (₹)",
                        "root_cause": "Diagnosed Root Cause",
                        "recommended_action": "Executed Tool",
                        "iterations_used": "Iterations",
                        "execution_status": "Agent Status",
                        "recovered": "Recovered?",
                        "recovered_amount": "Recovered ₹"
                    }
                ).style.format({
                    "Amount (₹)": "₹{:,.2f}",
                    "Recovered ₹": "₹{:,.2f}"
                }),
                use_container_width=True
            )
        else:
            st.info("No webhook-triggered agent runs recorded yet. Trigger a `payment.failed` event from the Razorpay dashboard or test suite.")
    else:
        st.info("No Razorpay webhook events received yet. Start `uvicorn app.main:app --port 8000` and configure your Razorpay Test Mode webhook URL.")

    # Phase 6 & 6.1 Additive: Real Razorpay Payment Links Recovery Attempts
    st.markdown("---")
    st.markdown("### 🔗 Real Razorpay Payment Links (Phase 6.1 Hardened Test-Mode Attempts)")
    st.markdown("""
    Payment Links generated dynamically via Razorpay's Test Mode API.
    **Accounting Rule:** Link created $\rightarrow$ `recovery_pending` (₹0.00 recovered).
    Revenue is officially recovered **only** after a verified `payment_link.paid`, `payment.captured`, or `order.paid` event.
    """)

    if df_attempts is not None and not df_attempts.empty:
        # Build display columns safely
        display_cols = [
            "transaction_id", "payment_id", "payment_link_id", "amount", "currency",
            "status", "verification_source", "created_at", "recovered_at", "payment_link_url"
        ]
        available_cols = [c for c in display_cols if c in df_attempts.columns]

        st.dataframe(
            df_attempts[available_cols].rename(
                columns={
                    "transaction_id": "Transaction ID",
                    "payment_id": "Razorpay Payment ID",
                    "payment_link_id": "Payment Link ID",
                    "amount": "Amount (₹)",
                    "currency": "Currency",
                    "status": "Recovery Status",
                    "verification_source": "Verification Source",
                    "created_at": "Created At",
                    "recovered_at": "Recovered At",
                    "payment_link_url": "Payment URL"
                }
            ).style.format({
                "Amount (₹)": "₹{:,.2f}"
            }),
            use_container_width=True
        )
    else:
        st.info("No Razorpay Payment Links created yet. When the Agent handles a failed transaction, real test-mode links (https://rzp.io/i/...) will appear here.")
