"""
agents/mcp_query_agent.py
─────────────────────────
MongoDB Natural Language Query Agent
Gemini decides what to query, Motor executes directly against Atlas.

Flow:
  1. Gemini reads the question and generates a MongoDB query
  2. Motor executes the query directly against Atlas
  3. Gemini formats the result as a natural language answer
  4. Reply sent to user

Security: every query filtered by user _id.
"""

import os
import json
from bson import ObjectId
from google import genai
from google.genai import types
from core.database import users_col, pockets_col, transactions_col
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


# ─────────────────────────────────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────────────────────────────────

def _get_time_ranges() -> dict:
    """Return pre-computed time range boundaries for common periods."""
    now   = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    week_start = today - timedelta(days=now.weekday())

    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    if now.month == 1:
        last_month_start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
    else:
        last_month_start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)

    return {
        "today":            today.isoformat(),
        "week_start":       week_start.isoformat(),
        "month_start":      month_start.isoformat(),
        "last_month_start": last_month_start.isoformat(),
        "month_end":        month_start.isoformat(),
        "now":              now.isoformat(),
        "today_str":        now.strftime("%Y-%m-%d"),
    }


# ─────────────────────────────────────────────────────────────────────
#  FIX OIDS IN QUERY
# ─────────────────────────────────────────────────────────────────────

def _fix_oids(obj):
    """Recursively convert {$oid: str} to ObjectId and ISO strings to datetime."""
    if isinstance(obj, dict):
        if "$oid" in obj:
            return ObjectId(obj["$oid"])
        if "$date" in obj:
            try:
                return datetime.fromisoformat(obj["$date"].replace("Z", "+00:00"))
            except Exception:
                return obj["$date"]
        return {k: _fix_oids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fix_oids(i) for i in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────────────────────────────

async def handle(
    sender:        str,
    question:      str,
    user:          dict,
    language:      str,
    language_name: str,
    send_message
):
    user_id   = user.get("_id")
    user_name = user.get("name", "")
    currency  = user.get("base_currency", "PKR")
    times     = _get_time_ranges()

    # ── Step 1: Ask Gemini what to query ──────────────────────────
    prompt = (
        f"Return ONLY a JSON object. No explanation. No markdown.\n\n"
        f"Question: {question}\n"
        f"User ObjectId: {str(user_id)}\n"
        f"Currency: {currency}\n\n"
        f"TIME REFERENCES (use these exact ISO strings for date filters):\n"
        f"  today starts at:      {times['today']}\n"
        f"  this week starts at:  {times['week_start']}\n"
        f"  this month starts at: {times['month_start']}\n"
        f"  last month starts at: {times['last_month_start']}\n"
        f"  right now:            {times['now']}\n\n"
        f"Collections:\n"
        f"- transactions: user_id (ObjectId), amount_base, merchant, pocket_id, timestamp, status\n"
        f"- pockets: user_id (ObjectId), name, slug, allocated_budget, current_balance, is_active\n\n"
        f"For date filters use: {{\"$date\": \"ISO_STRING_FROM_ABOVE\"}}\n\n"
        f"Examples:\n\n"
        f"Last transaction:\n"
        f'{{"type":"find","collection":"transactions","filter":{{"user_id":{{"$oid":"{str(user_id)}"}},"status":"confirmed"}},"sort":{{"timestamp":-1}},"limit":1}}\n\n'
        f"Spent today:\n"
        f'{{"type":"aggregate","collection":"transactions","pipeline":[{{"$match":{{"user_id":{{"$oid":"{str(user_id)}"}},"status":"confirmed","timestamp":{{"$gte":{{"$date":"{times["today"]}"}}}}}}}},{{"$group":{{"_id":null,"total":{{"$sum":"$amount_base"}}}}}}]}}\n\n'
        f"Spent this month:\n"
        f'{{"type":"aggregate","collection":"transactions","pipeline":[{{"$match":{{"user_id":{{"$oid":"{str(user_id)}"}},"status":"confirmed","timestamp":{{"$gte":{{"$date":"{times["month_start"]}"}}}}}}}},{{"$group":{{"_id":null,"total":{{"$sum":"$amount_base"}}}}}}]}}\n\n'
        f"Pocket balances:\n"
        f'{{"type":"find","collection":"pockets","filter":{{"user_id":{{"$oid":"{str(user_id)}"}},"is_active":true}},"sort":{{"current_balance":-1}},"limit":10}}\n\n'
        f"Second last transaction (limit 2, pick second):\n"
        f'{{"type":"find","collection":"transactions","filter":{{"user_id":{{"$oid":"{str(user_id)}"}},"status":"confirmed"}},"sort":{{"timestamp":-1}},"limit":2}}\n\n'
        f"Now return ONLY the JSON for: {question}"
    )

    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=1024)
        )
        raw = (resp.text or "").strip()
        if "```" in raw:
            parts = raw.split("```")
            raw   = parts[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        query = json.loads(raw.strip())
        print(f"NL QUERY: {json.dumps(query)[:200]}")
    except Exception as e:
        print(f"NL QUERY GENERATION ERROR: {e}")
        await send_message(sender,
            "I couldn't figure out how to answer that. "
            "Try: _balance_, _last transaction_, or _monthly summary_"
        )
        return

    # ── Step 2: Execute query with Motor ──────────────────────────
    try:
        col_map = {
            "transactions": transactions_col,
            "pockets":      pockets_col,
            "users":        users_col
        }
        col = col_map.get(query.get("collection"), transactions_col)

        if query["type"] == "find":
            filt  = _fix_oids(query.get("filter", {}))
            sort  = list(query.get("sort", {}).items())
            limit = int(query.get("limit", 10))
            cursor = col.find(filt)
            if sort:
                cursor = cursor.sort(sort)
            cursor  = cursor.limit(limit)
            results = await cursor.to_list(limit)
            data    = json.loads(json.dumps(results, default=str))

        elif query["type"] == "aggregate":
            pipeline = _fix_oids(query.get("pipeline", []))
            results  = await col.aggregate(pipeline).to_list(100)
            data     = json.loads(json.dumps(results, default=str))

        else:
            data = []

        print(f"QUERY RESULT: {str(data)[:200]}")

    except Exception as e:
        print(f"QUERY EXECUTION ERROR: {e}")
        data = {"error": str(e)}

    # ── Step 3: Gemini formats the answer ─────────────────────────
    lang_note = (
        f"Reply in {language_name} ({language})."
        if language not in ("en", "english") else ""
    )

    answer_prompt = (
        f"You are Micro-Pockets, a WhatsApp finance assistant.\n"
        f"Answer the user's question based on the MongoDB data.\n\n"
        f"User: {user_name}\n"
        f"Currency: {currency}\n"
        f"Question: {question}\n"
        f"Data: {json.dumps(data, default=str)}\n\n"
        f"{lang_note}\n\n"
        f"Rules:\n"
        f"- Concise — this is WhatsApp\n"
        f"- Use actual data to answer\n"
        f"- If empty list or zero total, say no transactions found\n"
        f"- Max 150 words\n"
        f"- Clean text with emojis, no markdown headers"
    )

    try:
        ans   = client.models.generate_content(
            model=MODEL,
            contents=answer_prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=300)
        )
        reply = (ans.text or "").strip()
        print(f"NL AGENT REPLY: {reply[:100]}")
        await send_message(sender, reply)
    except Exception as e:
        print(f"NL ANSWER ERROR: {e}")
        await send_message(sender, "I had trouble formatting that answer. Try asking differently.")