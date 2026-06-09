"""
agents/ingestion_agent.py
─────────────────────────
Ingestion Agent — handles automated bank SMS transactions.

Priority order for pocket matching:
  1. Custom merchant map  (user-taught, stored in users.merchant_map)
  2. Built-in keyword map (KFC→food, Careem→transport etc.)
  3. Gemini smart guess   (asks Gemini to pick the best pocket)
  4. Ask user             (no match found at all)

After confirmation of a Gemini-guessed transaction, offers to
remember the merchant→pocket mapping permanently.
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
    """Match merchant to pocket slug via built-in keyword map."""
    if not merchant:
        return None
    m = merchant.lower()
    for keyword, slug in MERCHANT_POCKET_MAP.items():
        if keyword in m:
            if any(p["slug"] == slug for p in pockets):
                return slug
    # Fuzzy match against user's actual pocket slugs
    for p in pockets:
        if p["slug"] in m or m in p["slug"]:
            return p["slug"]
    return None


# ─────────────────────────────────────────────────────────────────────
#  CUSTOM MERCHANT MAP
# ─────────────────────────────────────────────────────────────────────

async def _get_custom_map(user_id) -> dict:
    """Load user's saved merchant→pocket mappings from their profile."""
    user = await users_col.find_one({"_id": user_id})
    return user.get("merchant_map", {}) if user else {}


async def save_merchant_mapping(user_id, merchant: str, slug: str):
    """Permanently save a merchant→pocket mapping to the user's profile."""
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
    """Ask Gemini to pick the most likely pocket for this merchant."""
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
        guess = response.text.strip()
        print(f"GEMINI GUESS for '{merchant}': {guess}")

        for p in pockets:
            if p["name"].lower() == guess.lower():
                return p["slug"]
        return None

    except Exception as e:
        print(f"GEMINI GUESS error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
#  TRANSACTION INSERT HELPER
# ─────────────────────────────────────────────────────────────────────

async def _insert_pending(
    user_id, pocket: dict,
    merchant: str, amount: float,
    currency: str, raw: str
) -> str:
    """Insert a pending_review transaction and return its string ID."""
    result = await transactions_col.insert_one({
        "user_id":           user_id,
        "pocket_id":         pocket["_id"],
        "merchant":          merchant or "Bank transaction",
        "amount_base":       amount,
        "original_currency": currency,
        "original_amount":   amount,
        "exchange_rate":     1.0,
        "raw_payload":       raw,
        "timestamp":         datetime.now(timezone.utc),
        "status":            "pending_review",
        "source":            "bank_sms"
    })
    return str(result.inserted_id)


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
    Tries 4 matching strategies in order and notifies user.
    """
    amount   = intent.get("amount")
    currency = intent.get("currency") or user.get("base_currency", "PKR")
    merchant = intent.get("merchant", "")
    user_id  = user["_id"]

    if not amount:
        print(f"INGESTION: No amount found for {sender}")
        return

    # Load user's active pockets
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

    # ── 3. Gemini smart guess ──────────────────────────────────────────
    if not matched_pocket and merchant:
        guessed_slug = await _gemini_guess(merchant, pockets)
        if guessed_slug:
            matched_pocket = next(
                (p for p in pockets if p["slug"] == guessed_slug), None
            )
            if matched_pocket:
                match_source = "gemini"

    # ── 4a. Matched — insert and notify ───────────────────────────────
    if matched_pocket:
        txn_id      = await _insert_pending(
            user_id, matched_pocket,
            merchant, amount, currency,
            intent.get("raw", "")
        )
        new_balance = matched_pocket["current_balance"] - amount

        update = {"pending_txn_id": txn_id}

        # For Gemini guesses store learn data — shown after confirm
        if match_source == "gemini":
            update["pending_merchant_learn"] = {
                "merchant": merchant,
                "slug":     matched_pocket["slug"],
                "name":     matched_pocket["name"]
            }

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": update}
        )

        label = {
            "custom":  f"Matched to: *{matched_pocket['name']}* _(remembered)_",
            "keyword": f"Matched to: *{matched_pocket['name']}*",
            "gemini":  f"Best guess: *{matched_pocket['name']}*"
        }.get(match_source, f"Matched to: *{matched_pocket['name']}*")

        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — *{merchant or 'transaction'}*\n"
            f"{label}\n"
            f"Balance after: *{new_balance:.2f} {currency}*\n\n"
            "Reply *ok* to confirm or *move [pocket name]* to reassign."
        )

    # ── 4b. No match — ask user ────────────────────────────────────────
    else:
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — *{merchant or 'transaction'}*\n\n"
            f"Which pocket should I add this to?\n{pocket_list}\n\n"
            "_Reply with the pocket name_"
        )
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":   "add_expense",
                "amount":   amount,
                "currency": currency,
                "merchant": merchant,
                "source":   "bank_sms"
            }}}
        )