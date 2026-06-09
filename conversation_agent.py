"""
conversation_agent.py
─────────────────────
Conversation Agent — fully conversational financial assistant.

Handles intent = "conversational_query" or any message that needs
context-aware natural language response.

Gets:
  - Last 10 messages from conversations collection
  - User's full financial snapshot (income, pockets, transactions)
  - Current question
  - Detected language

Returns a natural language response in the SAME language as the question.

Covers:
  - "Why did I spend so much on food?"
  - "Compare this month vs last month"
  - "What did I buy last Tuesday?"
  - "Am I going to run out of transport budget?"
  - "Give me tips to save more"
  - Any open-ended financial question
"""

import os
import json
from google import genai
from google.genai import types
from database import users_col, pockets_col, transactions_col, conversations_col
from bson import ObjectId
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")


# ─────────────────────────────────────────────────────────────────────
#  BUILD FINANCIAL SNAPSHOT
# ─────────────────────────────────────────────────────────────────────

async def _build_snapshot(user: dict) -> dict:
    """
    Build a complete financial snapshot to give Gemini full context.
    Includes pockets, recent transactions, and month comparisons.
    """
    from mcp_tools import aggregate_monthly_trends, aggregate_monthly_trends_for

    user_id  = user["_id"]
    currency = user.get("base_currency", "PKR")
    now      = datetime.now(timezone.utc)

    # Pockets
    pockets = await pockets_col.find(
        {"user_id": user_id, "is_active": True}
    ).to_list(20)

    pockets_data = []
    for p in pockets:
        spent = p["allocated_budget"] - p["current_balance"]
        pct   = (spent / p["allocated_budget"] * 100) if p["allocated_budget"] else 0
        pockets_data.append({
            "name":      p["name"],
            "budget":    p["allocated_budget"],
            "spent":     max(spent, 0),
            "remaining": p["current_balance"],
            "pct_used":  round(pct, 1)
        })

    # Recent transactions — last 20
    txns = await transactions_col.find(
        {"user_id": user_id, "status": "confirmed"}
    ).sort("timestamp", -1).to_list(20)

    txns_data = []
    for t in txns:
        txns_data.append({
            "date":     t["timestamp"].strftime("%Y-%m-%d %H:%M"),
            "amount":   t["amount_base"],
            "merchant": t.get("merchant", ""),
            "pocket":   str(t.get("pocket_id", ""))
        })

    # This month trends
    current_trends = await aggregate_monthly_trends(str(user_id))

    # Last month trends
    last_month_trends = await aggregate_monthly_trends_for(
        str(user_id),
        month=now.month - 1 if now.month > 1 else 12,
        year=now.year if now.month > 1 else now.year - 1
    )

    return {
        "user_name":    user.get("name", ""),
        "currency":     currency,
        "monthly_income": user.get("financial_profile", {}).get("monthly_income", 0),
        "advisor_mode": user.get("settings", {}).get("advisor_mode", "off"),
        "pockets":      pockets_data,
        "recent_transactions": txns_data,
        "this_month":   current_trends,
        "last_month":   last_month_trends,
        "current_date": now.strftime("%Y-%m-%d"),
        "current_month": now.strftime("%B %Y")
    }


# ─────────────────────────────────────────────────────────────────────
#  BUILD CONVERSATION HISTORY
# ─────────────────────────────────────────────────────────────────────

async def _get_history(sender: str, limit: int = 10) -> list:
    """Fetch last N messages for conversation context."""
    msgs = await conversations_col.find(
        {"whatsapp_number": sender}
    ).sort("timestamp", -1).to_list(limit)

    # Reverse to chronological order
    msgs.reverse()

    history = []
    for m in msgs:
        role    = "user" if m["direction"] == "inbound" else "assistant"
        history.append({"role": role, "content": m["body"]})

    return history


# ─────────────────────────────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────────────────────────────

async def handle(
    sender: str,
    question: str,
    user: dict,
    language: str,
    language_name: str,
    send_message
):
    """
    Answer a conversational question with full financial context.
    Replies in the same language the question was asked in.
    """
    snapshot = await _build_snapshot(user)
    history  = await _get_history(sender, limit=10)

    lang_instruction = (
        f"IMPORTANT: The user is speaking in {language_name} ({language}). "
        f"You MUST respond entirely in {language_name}. "
        f"Do not switch to English unless the user switches first."
    ) if language not in ("en", "english") else ""

    system_prompt = f"""
You are Micro-Pockets, a personal finance assistant on WhatsApp.
You help users understand and manage their spending.
Be concise, friendly, and conversational — this is WhatsApp, not a report.
Use emojis naturally. Keep responses under 300 words.
{lang_instruction}

USER FINANCIAL DATA:
{json.dumps(snapshot, indent=2, default=str)}

CONVERSATION HISTORY (last 10 messages):
{json.dumps(history, indent=2)}

GUIDELINES:
- Answer directly based on the actual data above
- For month comparisons use this_month vs last_month data
- If data is missing say so honestly
- For spending questions reference actual transaction history
- Give actionable tips not just observations
- Never make up numbers — only use data provided
- If user asks something outside finance, gently redirect
""".strip()

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
                max_output_tokens=1024
            )
        )

        reply = response.text.strip()
        print(f"CONVERSATION: {reply[:100]}")
        await send_message(sender, reply)

    except Exception as e:
        print(f"CONVERSATION AGENT error: {e}")
        await send_message(sender,
            "Sorry, I had trouble answering that. "
            "Try asking again or type *help* to see what I can do."
        )