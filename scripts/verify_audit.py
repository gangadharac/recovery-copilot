import sys
import sqlite3
import json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect('data/recovery_audit.db')
cursor = conn.cursor()

print("================================================================")
print("🔍 1. SANITY CHECK: CARD_EXPIRED TRANSACTIONS")
print("================================================================")
cursor.execute('''
    SELECT transaction_id, amount, original_action, recommended_action, execution_status, recovered, gateway_switch_used, execution_notes
    FROM audit_logs
    WHERE root_cause = 'card_expired'
''')
rows = cursor.fetchall()
for r in rows:
    print(f"Txn: {r[0]} | Amt: INR {r[1]:>8.2f} | Orig: {r[2]} -> Decided: {r[3]:<16} | Status: {r[4]:<20} | Rec: {bool(r[5])}")
    print(f"   Notes: {r[7]}\n")

print("================================================================")
print("🛡️ 2. SANITY CHECK: RISK_BLOCKED TRANSACTIONS (0% By Design)")
print("================================================================")
cursor.execute('''
    SELECT transaction_id, amount, recommended_action, recovered, execution_status, strategy_reasoning
    FROM audit_logs
    WHERE root_cause = 'risk_blocked'
''')
risk_rows = cursor.fetchall()
for r in risk_rows:
    print(f"Txn: {r[0]} | Amt: INR {r[1]:>8.2f} | Action: {r[2]:<15} | Recovered: {bool(r[3])} | Status: {r[4]}")
    print(f"   Reasoning: {r[5]}\n")

print("================================================================")
print("📱 3. HINGLISH NUDGE SAMPLES (BANK TIMEOUT vs OTP vs FUNDS)")
print("================================================================")
cursor.execute('''
    SELECT DISTINCT root_cause, customer_name, amount, nudge_message
    FROM audit_logs
    WHERE nudge_message IS NOT NULL
    GROUP BY root_cause
''')
nudge_rows = cursor.fetchall()
for r in nudge_rows:
    print(f"[{r[0].upper()}] For {r[1]} (INR {r[2]:,.2f}):")
    print(f"   💬 \"{r[3]}\"\n")
