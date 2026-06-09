"""
smart_agent.py
──────────────
Smart Agent — fully capable fallback for unknown intents.

When the interpreter cannot classify a message, this agent:
  1. Pulls the user's complete financial snapshot from MongoDB
  2. Fetches the last 6 conversation messages for context
  3. Sends everything to Gemini with a structured prompt
  4. Gemini returns either:
       { "type": "answer",  "message": "..." }
       { "type": "action",  "intent": "...", ... }
  5. "answer" → sent directly to WhatsApp
     "action" → synthetic intent → interaction_agent.handle()

This means any message the interpreter misses — casual questions,
open-ended analysis, or naturally-phrased commands — gets handled
intelligently instead of falling back to the help menu.
"""

import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv
from database import pockets_col
from bson import ObjectId
from datetime import datetime, timezone

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")


# ─────────────────────────────────────────────────────────────────────
#  DATA SNAPSHOT
# ─────────────────────────────────────────────────────────────────────

async def _build_snapshot(user: dict) -> dict:
    """
    Collect all relevant financial data for this user into one dict.
    This is what gets passed to Gemini as context.
    """
    from mcp_tools import aggregate_today_spending, aggregate_monthly_trends

    user_id  = str(user["_id"])
    currency = user.get("base_currency", "PKR")
    income   = user.get("financial_profile", {}).get("monthly_income", 0)

    # Active pockets
    pockets_raw = await pockets_col.find(
        {"user_id": ObjectId(user_id), "is_active": True}
    ).to_list(20)

    pockets = []
    for p in pockets_raw:
        allocated = p["allocated_budget"]
        balance   = p["current_balance"]
        spent     = allocated - balance
        pockets.append({
            "name":      p["name"],
            "slug":      p["slug"],
            "budget":    allocated,
            "spent":     round(max(spent, 0), 2),
            "balance":   round(balance, 2),
            "pct_used":  round((spent / allocated * 100) if allocated else 0, 1),
            "over":      balance < 0
        })

    # Today's spending
    today_raw   = await aggregate_today_spending(user_id)
    today_total = sum(r["total_spent"] for r in today_raw)
    today = {
        "total": round(today_total, 2),
        "by_pocket": [
            {
                "pocket": r["pocket_name"],
                "spent":  round(r["total_spent"], 2),
                "count":  r["count"]
            }
            for r in today_raw
        ]
    }

    # Monthly trends
    monthly_raw   = await aggregate_monthly_trends(user_id)
    monthly_total = sum(r["total_spent"] for r in monthly_raw)
    now           = datetime.now(timezone.utc)
    monthly = {
        "month":       now.strftime("%B %Y"),
        "total_spent": round(monthly_total, 2),
        "by_pocket": [
            {
                "pocket":  r["pocket_name"],
                "spent":   round(r["total_spent"], 2),
                "budget":  round(r["allocated_budget"], 2),
                "pct_used": round(r.get("pct_used", 0), 1)
            }
            for r in monthly_raw
        ]
    }

    # 50/30/20 health
    budget_health = {}
    if income:
        daily_slice = round(income / 30, 2)
        budget_health = {
            "monthly_income":  income,
            "daily_slice":     daily_slice,
            "needs_target":    round(income * 0.50, 2),
            "wants_target":    round(income * 0.30, 2),
            "savings_target":  round(income * 0.20, 2),
            "on_track":        monthly_total <= income * 0.80,
            "today_vs_daily":  round(today_total - daily_slice, 2)
        }

    return {
        "name":          user.get("name", ""),
        "currency":      currency,
        "advisor_mode":  user.get("settings", {}).get("advisor_mode", "on_request"),
        "pockets":       pockets,
        "today":         today,
        "monthly":       monthly,
        "budget_health": budget_health
    }


# ─────────────────────────────────────────────────────────────────────
#  SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are the Smart Agent for Micro-Pockets, a WhatsApp personal finance tracker.
You are called when the system could not classify the user's message into a known intent.

Your job: understand what the user wants and respond with a strict JSON object.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE TYPES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TYPE 1 — Answer a question or give advice:
{
  "type":    "answer",
  "message": "Your WhatsApp reply here"
}

TYPE 2 — Trigger an action (user wants to DO something):
{
  "type":     "action",
  "intent":   "<one of the intents below>",
  "amount":   <float or null>,
  "currency": "<string or null>",
  "pocket":   "<slug or null>",
  "merchant": "<string or null>"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE ACTION INTENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
add_expense      → log a spend
delete_expense   → undo last transaction
query_balance    → show pocket balances
query_today      → show today's spending
monthly_summary  → show this month's summary
create_pocket    → create a new budget pocket
update_budget    → change a pocket's budget limit
rename_pocket    → rename a pocket
delete_pocket    → remove a pocket
add_income       → update monthly income
set_advisor_mode → change advisor setting (merchant = "off"|"proactive"|"on_request")
confirm          → confirm a pending transaction
cancel           → cancel a pending transaction
help             → show command guide

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POCKET SLUG RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Always match pockets from the user's data.
Default slugs: food, transport, bills, shopping
Custom slugs: lowercase, spaces replaced with hyphens
  "Cat Food" → cat-food, "Thailand Trip" → thailand-trip

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANSWER RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Keep answers SHORT and conversational — this is WhatsApp, not a report.
- Use the user's name if available.
- Use actual numbers from the user's data snapshot.
- Use emojis naturally but don't overdo it.
- Use *bold* for key numbers (WhatsApp markdown).
- If the user is off track, be honest but kind.
- If the user asks something completely unrelated to finance, reply with a short
  friendly message and gently steer back: "I'm your finance buddy — ask me about
  your spending, pockets, or budget!"
- NEVER make up numbers. Only use what's in the snapshot.
- NEVER return markdown code blocks. Return raw JSON only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AMOUNT PARSING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1k=1000, 2.5k=2500, 1m=1000000
Extract currency from symbols: $=USD, €=EUR, £=GBP, default=user's currency

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

User: "was today a good day financially?"
→ {"type":"answer","message":"Not bad, Hassan! You spent *2,400 PKR* today..."}

User: "should I eat out tonight?"
→ {"type":"answer","message":"Your food pocket is at 85% — you've got *750 PKR* left..."}

User: "am I saving enough?"
→ {"type":"answer","message":"Your savings target is *12,000 PKR/month*. So far..."}

User: "just put 500 on food"
→ {"type":"action","intent":"add_expense","amount":500.0,"currency":"PKR","pocket":"food","merchant":null}

User: "log 350 uber"
→ {"type":"action","intent":"add_expense","amount":350.0,"currency":"PKR","pocket":"transport","merchant":"Uber"}

User: "what's my food budget again"
→ {"type":"action","intent":"query_balance","amount":null,"currency":null,"pocket":"food","merchant":null}

User: "make a travel pocket with 10k"
→ {"type":"action","intent":"create_pocket","amount":10000.0,"currency":"PKR","pocket":"travel","merchant":null}

User: "tell me a joke"
→ {"type":"answer","message":"Ha! I'm better with budgets than jokes 😄 Ask me how your spending is going!"}
""".strip()


# ─────────────────────────────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────────────━━━━━━━━━━━━━━━

async def handle(
    sender:       str,
    raw_message:  str,
    user:         dict,
    send_message  # callable from main.py
):
    """
    Called when interpreter returns intent='unknown'.
    Pulls full user snapshot + recent conversation history,
    asks Gemini what to do, then either answers or triggers an action.
    """
    from mcp_tools import get_recent_conversations
    from interaction_agent import handle as interaction_handle

    # ── 1. Build context ─────────────────────────────────────────────
    snapshot     = await _build_snapshot(user)
    recent_convs = await get_recent_conversations(sender, limit=6)

    # Format conversation history as readable text for the prompt
    history_lines = []
    for m in recent_convs:
        role = "User" if m["direction"] == "inbound" else "Bot"
        history_lines.append(f"[{m['timestamp']}] {role}: {m['body']}")
    history_text = "\n".join(history_lines) if history_lines else "No previous messages."

    # ── 2. Build the user message for Gemini ─────────────────────────
    user_prompt = (
        "USER DATA SNAPSHOT:\n"
        + json.dumps(snapshot, indent=2)
        + "\n\nRECENT CONVERSATION:\n"
        + history_text
        + "\n\nUSER'S CURRENT MESSAGE:\n"
        + raw_message
    )

    # ── 3. Call Gemini ────────────────────────────────────────────────
    fallback_answer = (
        "I didn't quite get that. Try:\n"
        "• _spent 500 on food_\n"
        "• _balance_\n"
        "• _how am I doing_\n"
        "• _help_"
    )

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=512
            )
        )

        raw_text = response.text.strip()
        print(f"SMART AGENT RAW: {raw_text}")

        # Strip markdown fences if Gemini adds them
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        result = json.loads(raw_text)
        print(f"SMART AGENT RESULT: {result}")

    except json.JSONDecodeError as e:
        print(f"SMART AGENT JSON error: {e}")
        await send_message(sender, fallback_answer)
        return
    except Exception as e:
        print(f"SMART AGENT error: {e}")
        await send_message(sender, fallback_answer)
        return

    # ── 4. Execute result ─────────────────────────────────────────────
    result_type = result.get("type")

    if result_type == "answer":
        message = result.get("message", "").strip()
        if message:
            await send_message(sender, message)
        else:
            await send_message(sender, fallback_answer)

    elif result_type == "action":
        # Build a synthetic intent and pass to interaction_agent
        synthetic_intent = {
            "intent":   result.get("intent", "unknown"),
            "amount":   result.get("amount"),
            "currency": result.get("currency") or user.get("base_currency", "PKR"),
            "pocket":   result.get("pocket"),
            "merchant": result.get("merchant"),
            "raw":      raw_message
        }
        print(f"SMART AGENT → ACTION: {synthetic_intent['intent']}")
        await interaction_handle(sender, synthetic_intent, user, send_message)

    else:
        # Gemini returned something unexpected
        await send_message(sender, fallback_answer)