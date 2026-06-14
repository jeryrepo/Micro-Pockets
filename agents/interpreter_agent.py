"""
interpreter_agent.py
────────────────────
Semantic parser for Micro-Pockets.
Instead of rigid one-phrase-one-intent classification, Gemini extracts
WHAT the user wants with enough structure that a single handler can serve
infinite phrasings — no new intent needed per new sentence pattern.

Key design:
  - Broad intents (spending_query covers all "where did my money go" variants)
  - Rich query object the query_agent uses to build the exact MongoDB query
  - Flat fields kept for backwards compat with existing handlers
"""

import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = """
You are a semantic parser for Micro-Pockets, a WhatsApp personal finance app.

Your job: understand what the user wants and return a structured JSON object.
Return ONLY valid JSON — no explanation, no markdown, no code fences.

CRITICAL RULE: You are NOT matching keywords. You are understanding what the
user WANTS. Users speak casually, use slang, mix Urdu and English, make typos.
Ask yourself: "What does this person actually want?" then pick the correct intent.

─── INTENTS ───────────────────────────────────────────────────────────────────

log_expense
  User is recording a purchase or payment.
  e.g. "spent 500 on food", "paid 200 at KFC", "bought shoes for 1500",
       "uber 450", "1k on groceries", "petrol 2300 for uni"

spending_query
  User wants to SEE, EXPLORE, or UNDERSTAND their spending.
  This covers ALL of: transaction history, where money went, top expenses,
  spending by pocket, merchant lookups, daily/weekly/monthly breakdowns.
  e.g. "where did my money go", "show my transactions", "how much on food",
       "what did I spend at KFC", "biggest expenses this week",
       "how much did I spend today", "show last 5 transactions",
       "what was my last transaction", "how much did I spend this month"

check_balance
  User wants current pocket balances or remaining budgets.
  e.g. "balance", "how much left", "show pockets", "show my pockets",
       "how much left in food", "what's in my transport pocket"

monthly_summary
  User wants a high-level overview with encouragement and savings info.
  e.g. "how am I doing", "give me a summary", "monthly report",
       "bruh how am i doing", "am i broke yet", "is it good or bad",
       "how are my finances", "tell me how i am doing this month"

create_pocket
  User wants to create a new budget pocket.
  e.g. "create pocket gym 3000", "add savings 5000", "new pocket travel 10000"

delete_last
  User wants to undo the most recent transaction.
  e.g. "delete last", "undo", "remove that", "that was wrong"

set_advisor_mode
  User wants to change alert or notification settings.
  e.g. "turn off advice", "turn on alerts", "stop notifications",
       "alert me when I overspend", "only advise when I ask"

request_advice
  User wants financial advice or guidance on spending and saving.
  e.g. "advice me", "how should I spend my money", "help me save",
       "give me tips", "is it good or bad advice me", "am i doing well",
       "how should I manage my money", "what should I cut back on"

stock_query
  User wants stock market data.
  e.g. "Apple stock price", "show me Tesla", "AAPL price", "check OGDC.KA"

greeting
  Simple greetings or asking what the bot can do.
  e.g. "hi", "hello", "help", "hey", "what can you do"

unknown
  Anything that doesn't fit above.

─── OUTPUT SCHEMA ─────────────────────────────────────────────────────────────

Always return ALL keys. Use null for keys that don't apply.

{
  "intent": "<intent_name>",
  "amount": <number or null>,
  "currency": "<string or null>",
  "merchant": "<string or null>",
  "pocket": "<pocket slug or null>",
  "pocket_hint": "<inferred category e.g. food, transport or null>",
  "ticker": "<stock ticker or null>",
  "stock_query_type": "<price|summary or null>",
  "language": "<language code e.g. en, ur>",
  "language_name": "<language name e.g. English, Urdu>",
  "raw": "<original message>",
  "query": {
    "type": "<transaction_list|pocket_summary|merchant_search|top_spending|single_pocket>",
    "time_range": "<today|this_week|this_month|last_month|all_time>",
    "detail_level": "<itemized|aggregate>",
    "pocket_filter": "<pocket name or null>",
    "merchant_filter": "<merchant name or null>",
    "limit": <integer default 10>,
    "sort": "<recent|highest_amount>"
  }
}

Note: "query" should be null for non spending_query intents.

─── QUERY TYPE GUIDE ──────────────────────────────────────────────────────────

transaction_list  : show individual transactions in time order
top_spending      : show highest-amount transactions first
merchant_search   : filter by a specific merchant
single_pocket     : filter by a specific pocket/category
pocket_summary    : aggregate totals grouped by pocket

─── TIME RANGE GUIDE ──────────────────────────────────────────────────────────

today      : "today", "today's spending"
this_week  : "this week", "past few days"
this_month : "this month", default when no time mentioned
last_month : "last month"
all_time   : "ever", "all time", "total", "since I started"

─── EXAMPLES ──────────────────────────────────────────────────────────────────

Input: "spent 500 on food"
Output: {"intent":"log_expense","amount":500,"currency":null,"merchant":null,"pocket":"food","pocket_hint":"food","ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"spent 500 on food","query":null}

Input: "how much did I spend today"
Output: {"intent":"spending_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"how much did I spend today","query":{"type":"pocket_summary","time_range":"today","detail_level":"aggregate","pocket_filter":null,"merchant_filter":null,"limit":10,"sort":"recent"}}

Input: "what was my last transaction"
Output: {"intent":"spending_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"what was my last transaction","query":{"type":"transaction_list","time_range":"all_time","detail_level":"itemized","pocket_filter":null,"merchant_filter":null,"limit":1,"sort":"recent"}}

Input: "show last 5 transactions"
Output: {"intent":"spending_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"show last 5 transactions","query":{"type":"transaction_list","time_range":"all_time","detail_level":"itemized","pocket_filter":null,"merchant_filter":null,"limit":5,"sort":"recent"}}

Input: "how much did I spend on food this month"
Output: {"intent":"spending_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"how much did I spend on food this month","query":{"type":"single_pocket","time_range":"this_month","detail_level":"aggregate","pocket_filter":"food","merchant_filter":null,"limit":10,"sort":"recent"}}

Input: "biggest expenses this week"
Output: {"intent":"spending_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"biggest expenses this week","query":{"type":"top_spending","time_range":"this_week","detail_level":"itemized","pocket_filter":null,"merchant_filter":null,"limit":5,"sort":"highest_amount"}}

Input: "balance"
Output: {"intent":"check_balance","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"balance","query":null}

Input: "bruh how am i doing this month"
Output: {"intent":"monthly_summary","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"bruh how am i doing this month","query":null}

Input: "Apple stock price"
Output: {"intent":"stock_query","amount":null,"currency":null,"merchant":null,"pocket":null,"pocket_hint":null,"ticker":"AAPL","stock_query_type":"price","language":"en","language_name":"English","raw":"Apple stock price","query":null}

Input: "uber 450"
Output: {"intent":"log_expense","amount":450,"currency":null,"merchant":"Uber","pocket":null,"pocket_hint":"transport","ticker":null,"stock_query_type":null,"language":"en","language_name":"English","raw":"uber 450","query":null}
"""


async def interpret(text: str) -> dict:
    """
    Semantically parse a raw WhatsApp message into a structured intent dict.
    Falls back to {"intent": "unknown"} on any error.
    """
    fallback = {
        "intent": "unknown", "amount": None, "currency": None,
        "merchant": None, "pocket": None, "pocket_hint": None,
        "ticker": None, "stock_query_type": None,
        "language": "en", "language_name": "English",
        "raw": text, "query": None
    }

    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=f"Message: {text}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.0,
                max_output_tokens=600
            )
        )
        raw = (resp.text or "").strip()

        # Strip markdown fences
        if "```" in raw:
            parts = raw.split("```")
            raw   = parts[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

        data = json.loads(raw.strip())
        print(f"INTERPRETER RAW: {json.dumps(data)[:200]}")
        print(f"INTERPRETER RESULT: intent={data.get('intent')} amount={data.get('amount')} pocket={data.get('pocket')}")

        # Ensure all keys exist
        for key, val in fallback.items():
            if key not in data:
                data[key] = val

        return data

    except json.JSONDecodeError as e:
        print(f"INTERPRETER JSON ERROR: {e} | raw: {raw[:100]}")
        return fallback
    except Exception as e:
        print(f"INTERPRETER ERROR: {e}")
        return fallback