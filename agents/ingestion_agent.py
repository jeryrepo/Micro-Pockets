"""
agents/ingestion_agent.py
─────────────────────────
Ingestion Agent — handles automated bank SMS transactions.

Priority order for pocket matching:
  1. Custom merchant map  (user-taught)
  2. Built-in keyword map (KFC→food, Careem→transport etc.)
  3. Gemini smart guess
  4. Ask user

For ALL matched transactions — first ask user: deduct or add?
  deduct → expense, reduces pocket balance
  add    → income received, increases pocket balance
  discard → ignored
"""

import os
from google import genai
from google.genai import types
from core.database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")


# ─────────────────────────────────────────────────────────────────────
#  BUILT-IN MERCHANT KEYWORD MAP
# ─────────────────────────────────────────────────────────────────────

MERCHANT_POCKET_MAP = {
    # Food
    "kfc":        "food", "mcdonald":   "food", "mcdonalds":  "food",
    "burger":     "food", "pizza":      "food", "cafe":       "food",
    "coffee":     "food", "dunkin":     "food", "grocery":    "food",
    "groceries":  "food", "superstore": "food", "imtiaz":     "food",
    "carrefour":  "food", "metro":      "food", "noon":       "food",
    "restaurant": "food", "biryani":    "food", "hotel":      "food",
    "bakery":     "food", "sweets":     "food", "bbq":        "food",
    # Transport
    "uber":       "transport", "careem":    "transport", "petrol":  "transport",
    "fuel":       "transport", "shell":     "transport", "pso":     "transport",
    "bus":        "transport", "rickshaw":  "transport", "parking": "transport",
    "toll":       "transport", "caltex":    "transport", "total":   "transport",
    # Bills
    "lesco":      "bills", "sngpl":      "bills", "ptcl":     "bills",
    "jazz":       "bills", "telenor":    "bills", "ufone":    "bills",
    "zong":       "bills", "k-electric": "bills", "sui":      "bills",
    "water":      "bills", "hesco":      "bills", "mepco":    "bills",
    # Shopping
    "daraz":      "shopping", "amazon":    "shopping", "nike":     "shopping",
    "outfitters": "shopping", "khaadi":    "shopping", "sapphire": "shopping",
    "bata":       "shopping", "service":   "shopping",
}


def _keyword_match(merchant: str, pockets: list) -> str | None:
    if not merchant:
        return None
    m = merchant.lower()
    for keyword, slug in MERCHANT_POCKET_MAP.items():
        if keyword in m:
            if any(p["slug"] == slug for p in pockets):
                return slug
    for p in pockets:
        if p["slug"] in m or m in p["slug"]:
            return p["slug"]
    return None


# ─────────────────────────────────────────────────────────────────────
#  CUSTOM MERCHANT MAP
# ─────────────────────────────────────────────────────────────────────

async def _get_custom_map(user_id) -> dict:
    user = await users_col.find_one({"_id": user_id})
    return user.get("merchant_map", {}) if user else {}


async def save_merchant_mapping(user_id, merchant: str, slug: str):
    key = merchant.lower().strip()
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {f"merchant_map.{key}": slug}}
    )
    print(f"SAVED merchant map: {key} → {slug}")


# ─────────────────────────────────────────────────────────────────────
#  GEMINI SMART GUESS
# ─────────────────────────────────────────────────────────────────────

async def _gemini_guess(merchant: str, pockets: list) -> str | None:
    pocket_names = [p["name"] for p in pockets]
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=(
                f"A bank transaction was made at '{merchant}'. "
                f"Which of these budget categories does it most likely belong to: "
                f"{', '.join(pocket_names)}? "
                f"Reply with ONLY the category name exactly as written. "
                f"If genuinely none fit, reply NONE."
            ),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=20
            )
        )
        guess = (response.text or "").strip()
        print(f"GEMINI GUESS for '{merchant}': {guess}")
        for p in pockets:
            if p["name"].lower() == guess.lower():
                return p["slug"]
        return None
    except Exception as e:
        print(f"GEMINI GUESS error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────────────────────────────

async def process(
    sender: str,
    intent: dict,
    user: dict,
    send_message
):
    """
    Process a bank SMS transaction.
    Always asks user: deduct or add? before touching any balance.
    """
    amount   = intent.get("amount")
    currency = intent.get("currency") or user.get("base_currency", "PKR")
    merchant = intent.get("merchant", "")
    user_id  = user["_id"]

    if not amount:
        print(f"INGESTION: No amount found for {sender}")
        return

    pockets = await pockets_col.find(
        {"user_id": user_id, "is_active": True}
    ).to_list(20)

    if not pockets:
        await send_message(sender,
            f"💳 *Bank transaction:* {amount:.0f} {currency} — {merchant}\n\n"
            "You have no pockets yet.\n"
            "Create one: _create pocket food 5000_"
        )
        return

    matched_pocket = None
    match_source   = None

    # ── 1. Custom merchant map ─────────────────────────────────────────
    custom_map  = await _get_custom_map(user_id)
    custom_slug = custom_map.get((merchant or "").lower().strip())
    if custom_slug:
        matched_pocket = next(
            (p for p in pockets if p["slug"] == custom_slug), None
        )
        if matched_pocket:
            match_source = "custom"

    # ── 2. Built-in keyword map ────────────────────────────────────────
    if not matched_pocket:
        kw_slug = _keyword_match(merchant, pockets)
        if kw_slug:
            matched_pocket = next(
                (p for p in pockets if p["slug"] == kw_slug), None
            )
            if matched_pocket:
                match_source = "keyword"

    # ── Build pending intent base ──────────────────────────────────────
    pending_base = {
        "action":   "awaiting_deduct_or_add",
        "amount":   amount,
        "currency": currency,
        "merchant": merchant,
        "source":   "bank_sms",
        "raw":      intent.get("raw", "")
    }

    if matched_pocket:
        # Pocket found — store it so we skip asking later
        pending_base["pocket_slug"] = matched_pocket["slug"]
        pending_base["pocket_name"] = matched_pocket["name"]
        if match_source == "gemini":
            pending_base["gemini_guess"] = True

        label = {
            "custom":  f"Suggested: *{matched_pocket['name']}* _(remembered)_",
            "keyword": f"Suggested: *{matched_pocket['name']}*",
            "gemini":  f"Best guess: *{matched_pocket['name']}*"
        }.get(match_source, f"Suggested: *{matched_pocket['name']}*")

        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — *{merchant or 'transaction'}*\n"
            f"{label}\n\n"
            "Was this money you *spent* or money you *received*?\n\n"
            "Reply *spent*, *received*, or *discard*"
        )
    else:
        # No pocket match
        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — *{merchant or 'transaction'}*\n\n"
            "Should I *deduct* this from a pocket or *add* it?\n\n"
            "Reply *deduct*, *add*, or *discard*"
        )

    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"pending_intent": pending_base}}
    )