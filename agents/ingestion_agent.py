"""
ingestion_agent.py
──────────────────
Ingestion Agent — handles automated bank SMS transactions.

Responsibilities:
  1. Receive parsed bank_sms intent from interpreter_agent.py
  2. Semantically match merchant/amount to a pocket
  3. Insert transaction as pending_review
  4. Notify user on WhatsApp to confirm

This agent runs in the background — triggered by MacroDroid / iOS Shortcut
posting to /webhook/bank-notifications
"""

from database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone


# Merchant → pocket slug mapping
# Extend this as you see more real bank SMS patterns
MERCHANT_POCKET_MAP = {
    # Food
    "kfc":        "food",
    "mcdonald":   "food",
    "mcdonalds":  "food",
    "burger":     "food",
    "pizza":      "food",
    "cafe":       "food",
    "coffee":     "food",
    "dunkin":     "food",
    "grocery":    "food",
    "groceries":  "food",
    "superstore": "food",
    "imtiaz":     "food",
    "carrefour":  "food",
    "metro":      "food",
    "noon":       "food",
    # Transport
    "uber":       "transport",
    "careem":     "transport",
    "petrol":     "transport",
    "fuel":       "transport",
    "shell":      "transport",
    "pso":        "transport",
    "bus":        "transport",
    "rickshaw":   "transport",
    # Bills
    "lesco":      "bills",
    "sngpl":      "bills",
    "ptcl":       "bills",
    "jazz":       "bills",
    "telenor":    "bills",
    "ufone":      "bills",
    "zong":       "bills",
    "k-electric": "bills",
    "sui":        "bills",
    # Shopping
    "daraz":      "shopping",
    "amazon":     "shopping",
    "nike":       "shopping",
    "outfitters": "shopping",
    "khaadi":     "shopping",
    "sapphire":   "shopping",
}


def match_pocket_slug(merchant: str, pockets: list) -> str | None:
    """
    Try to match a merchant name to a pocket slug.
    First checks MERCHANT_POCKET_MAP, then fuzzy matches against
    the user's actual pocket slugs.
    Returns slug string or None if no match found.
    """
    if not merchant:
        return None

    merchant_lower = merchant.lower()

    # Check keyword map first
    for keyword, slug in MERCHANT_POCKET_MAP.items():
        if keyword in merchant_lower:
            return slug

    # Fuzzy match against user's actual pocket slugs
    pocket_slugs = [p["slug"] for p in pockets]
    for slug in pocket_slugs:
        if slug in merchant_lower or merchant_lower in slug:
            return slug

    return None


async def process(
    sender: str,
    intent: dict,
    user: dict,
    send_message  # callable from main.py
):
    """
    Main entry point for bank SMS processing.
    Called when interpreter returns intent='bank_sms'.
    """
    amount   = intent.get("amount")
    currency = intent.get("currency") or user.get("base_currency", "PKR")
    merchant = intent.get("merchant")
    user_id  = user["_id"]

    if not amount:
        print(f"INGESTION: No amount found in SMS for {sender}")
        return

    # Load user's active pockets for matching
    pockets = await pockets_col.find(
        {"user_id": user_id, "is_active": True}
    ).to_list(20)

    if not pockets:
        await send_message(sender,
            f"💳 Bank transaction: *{amount:.0f} {currency}*\n"
            f"From: {merchant or 'unknown'}\n\n"
            "You have no active pockets yet. "
            "Create one: _create pocket food 5000_"
        )
        return

    # Try semantic pocket matching
    matched_slug = match_pocket_slug(merchant, pockets)
    matched_pocket = None

    if matched_slug:
        matched_pocket = next(
            (p for p in pockets if p["slug"] == matched_slug), None
        )

    if matched_pocket:
        # Auto-matched — insert as pending_review and ask to confirm
        result = await transactions_col.insert_one({
            "user_id":           user_id,
            "pocket_id":         matched_pocket["_id"],
            "merchant":          merchant or "Bank transaction",
            "amount_base":       amount,
            "original_currency": currency,
            "original_amount":   amount,
            "exchange_rate":     1.0,
            "raw_payload":       intent.get("raw", ""),
            "timestamp":         datetime.now(timezone.utc),
            "status":            "pending_review",
            "source":            "bank_sms"
        })

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_txn_id": str(result.inserted_id)}}
        )

        new_balance = matched_pocket["current_balance"] - amount
        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — {merchant or 'transaction'}\n"
            f"Matched to: *{matched_pocket['name']}*\n"
            f"Balance after: *{new_balance:.2f} {currency}*\n\n"
            "Reply *ok* to confirm or *move [pocket]* to reassign."
        )

    else:
        # No match — ask user which pocket
        pocket_list = "\n".join([
            f"• *{p['name']}*" for p in pockets
        ])
        await send_message(sender,
            f"🏦 *Bank transaction detected*\n"
            f"{amount:.0f} {currency} — {merchant or 'transaction'}\n\n"
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