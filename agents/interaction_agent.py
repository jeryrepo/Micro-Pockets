"""
interaction_agent.py
────────────────────
Interaction Agent — handles all WRITE operations.

Responsibilities:
  add_expense      → log a transaction (pending_review → confirmed)
  delete_expense   → soft-delete last confirmed transaction
  create_pocket    → create a new budget pocket
  update_budget    → change pocket budget limit
  rename_pocket    → rename an existing pocket
  delete_pocket    → deactivate a pocket
  add_income       → update monthly income
  set_advisor_mode → change advisor notification preference
  confirm          → confirm a pending transaction
  cancel           → cancel a pending action

Never reads for display purposes — that is query_agent's job.
All balance updates are atomic using $inc to prevent race conditions.
"""

from core.database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone


async def handle(
    sender: str,
    intent: dict,
    user: dict,
    send_message
):
    """Main entry point — routes to correct write handler."""
    i = intent["intent"]

    if   i == "add_expense":       await _add_expense(sender, intent, user, send_message)
    elif i == "delete_expense":    await _delete_expense(sender, user, send_message)
    elif i == "create_pocket":     await _create_pocket(sender, intent, user, send_message)
    elif i == "update_budget":     await _update_budget(sender, intent, user, send_message)
    elif i == "rename_pocket":     await _rename_pocket(sender, intent, user, send_message)
    elif i == "delete_pocket":     await _delete_pocket(sender, intent, user, send_message)
    elif i == "add_income":        await _add_income(sender, intent, user, send_message)
    elif i == "set_advisor_mode":  await _set_advisor_mode(sender, intent, user, send_message)
    elif i == "confirm":           await _confirm(sender, user, send_message)
    elif i == "cancel":            await _cancel(sender, user, send_message)


# ─────────────────────────────────────────────────────────────────────
#  ADD EXPENSE
# ─────────────────────────────────────────────────────────────────────

async def _add_expense(
    sender: str,
    intent: dict,
    user: dict,
    send_message,
    source: str = "whatsapp"
):
    amount   = intent.get("amount")
    currency = intent.get("currency") or user.get("base_currency", "PKR")
    slug     = intent.get("pocket")
    merchant = intent.get("merchant")
    user_id  = user["_id"]

    if not amount:
        await send_message(sender,
            "I couldn't find an amount. Try: _spent 500 on food_")
        return

    pocket = None
    if slug:
        pocket = await pockets_col.find_one({
            "user_id": user_id, "slug": slug, "is_active": True
        })

    if not pocket:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)

        if not pockets:
            await send_message(sender,
                "You have no pockets yet.\n"
                "Create one first: _create pocket food 5000_")
            return

        pocket_list = "\n".join([
            f"• *{p['name']}* — {p['current_balance']:.0f} {currency} left"
            for p in pockets
        ])
        await send_message(sender,
            f"Which pocket for *{amount:.0f} {currency}*?\n\n"
            f"{pocket_list}\n\n_Reply with the pocket name_"
        )
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":   "add_expense",
                "amount":   amount,
                "currency": currency,
                "merchant": merchant,
                "source":   source
            }}}
        )
        return

    result = await transactions_col.insert_one({
        "user_id":           user_id,
        "pocket_id":         pocket["_id"],
        "merchant":          merchant or "Manual entry",
        "amount_base":       amount,
        "original_currency": currency,
        "original_amount":   amount,
        "exchange_rate":     1.0,
        "raw_payload":       intent.get("raw", ""),
        "timestamp":         datetime.now(timezone.utc),
        "status":            "pending_review",
        "source":            source
    })

    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"pending_txn_id": str(result.inserted_id)}}
    )

    new_balance  = pocket["current_balance"] - amount
    merchant_str = f" at *{merchant}*" if merchant else ""

    await send_message(sender,
        f"*{amount:.0f} {currency}*{merchant_str} → *{pocket['name']}*\n"
        f"Balance after: *{new_balance:.2f} {currency}*\n\n"
        "Reply *ok* to confirm or *cancel* to discard."
    )


# ─────────────────────────────────────────────────────────────────────
#  CONFIRM
# ─────────────────────────────────────────────────────────────────────

async def _confirm(sender: str, user: dict, send_message):
    from advisor_agent import check_alert

    txn_id = user.get("pending_txn_id")
    if not txn_id:
        # Silent ack — user said ok after something like advisor mode change
        await send_message(sender, "👍 Got it!")
        return

    txn = await transactions_col.find_one({"_id": ObjectId(txn_id)})
    if not txn or txn["status"] != "pending_review":
        await send_message(sender, "Transaction already processed.")
        return

    await transactions_col.update_one(
        {"_id": ObjectId(txn_id)},
        {"$set": {"status": "confirmed"}}
    )

    updated_pocket = await pockets_col.find_one_and_update(
        {"_id": txn["pocket_id"]},
        {"$inc": {"current_balance": -txn["amount_base"]}},
        return_document=True
    )

    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$unset": {"pending_txn_id": "", "pending_intent": ""}}
    )

    currency = user.get("base_currency", "PKR")
    await send_message(sender,
        f"✅ *{txn['amount_base']:.0f} {currency}* confirmed!\n"
        f"*{updated_pocket['name']}* remaining: "
        f"*{updated_pocket['current_balance']:.2f} {currency}*"
    )

    # ── Offer to remember merchant mapping if it was a Gemini guess ───
    learn_data = user.get("pending_merchant_learn", {})
    if learn_data and learn_data.get("merchant"):
        merchant    = learn_data["merchant"]
        pocket_name = updated_pocket.get("name", "")
        slug        = updated_pocket.get("slug", "")

        # Clear learn data
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$unset": {"pending_merchant_learn": ""}}
        )
        # Set pending intent for yes/no response
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":   "remember_merchant",
                "merchant": merchant,
                "slug":     slug,
                "name":     pocket_name
            }}}
        )
        await send_message(sender,
            f"💡 Always add *{merchant}* to *{pocket_name}*?\n"
            "_Reply *yes* to remember this_"
        )

    await check_alert(sender, user, updated_pocket, send_message)


# ─────────────────────────────────────────────────────────────────────
#  CANCEL
# ─────────────────────────────────────────────────────────────────────

async def _cancel(sender: str, user: dict, send_message):
    txn_id = user.get("pending_txn_id")
    if txn_id:
        await transactions_col.update_one(
            {"_id": ObjectId(txn_id)},
            {"$set": {"status": "cancelled"}}
        )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$unset": {"pending_txn_id": "", "pending_intent": ""}}
    )
    await send_message(sender, "Cancelled. What else can I help with?")


# ─────────────────────────────────────────────────────────────────────
#  DELETE EXPENSE
# ─────────────────────────────────────────────────────────────────────

async def _delete_expense(sender: str, user: dict, send_message):
    user_id = user["_id"]
    txn = await transactions_col.find_one(
        {"user_id": user_id, "status": "confirmed"},
        sort=[("timestamp", -1)]
    )

    if not txn:
        await send_message(sender,
            "No confirmed transactions found to delete.")
        return

    await transactions_col.update_one(
        {"_id": txn["_id"]},
        {"$set": {"status": "deleted"}}
    )

    restored = await pockets_col.find_one_and_update(
        {"_id": txn["pocket_id"]},
        {"$inc": {"current_balance": txn["amount_base"]}},
        return_document=True
    )

    currency = user.get("base_currency", "PKR")
    await send_message(sender,
        f"🗑️ Deleted *{txn['amount_base']:.0f} {currency}* "
        f"({txn.get('merchant', 'expense')})\n"
        f"*{restored['name']}* restored to: "
        f"*{restored['current_balance']:.2f} {currency}*"
    )


# ─────────────────────────────────────────────────────────────────────
#  CREATE POCKET
# ─────────────────────────────────────────────────────────────────────

async def _create_pocket(sender: str, intent: dict, user: dict, send_message):
    user_id  = str(user["_id"])
    slug     = intent.get("pocket") or ""
    name     = slug.replace("-", " ").title() if slug else ""
    budget   = intent.get("amount")
    currency = user.get("base_currency", "PKR")

    # No pocket name — ask for it
    if not slug or not name:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {"action": "awaiting_pocket_name"}}}
        )
        await send_message(sender,
            "What would you like to name this pocket?\n"
            "_e.g. Travel, Cat Food, Emergency Fund_"
        )
        return

    # Name provided but no budget — ask for it
    if not budget:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":      "awaiting_pocket_budget",
                "pocket_name": name,
                "pocket_slug": slug
            }}}
        )
        await send_message(sender,
            f"How much is the monthly budget for *{name}*?\n"
            f"_e.g. 5000 {currency}_"
        )
        return

    # Both name and budget — create it
    existing = await pockets_col.find_one({
        "user_id": ObjectId(user_id), "slug": slug, "is_active": True
    })
    if existing:
        await send_message(sender,
            f"You already have a *{name}* pocket.\n"
            f"To update its budget: _change {slug} budget to {budget:.0f}_")
        return

    await pockets_col.insert_one({
        "user_id":             ObjectId(user_id),
        "name":                name,
        "slug":                slug,
        "type":                "permanent",
        "allocated_budget":    budget,
        "current_balance":     budget,
        "alert_threshold_pct": 80,
        "alert_snoozed":       False,
        "snooze_reset_date":   None,
        "is_active":           True,
        "expires_at":          None,
        "created_at":          datetime.now(timezone.utc)
    })

    await send_message(sender,
        f"✅ *{name}* pocket created!\n"
        f"Budget: *{budget:.0f} {currency}*\n\n"
        f"Start logging: _spent 500 on {slug}_"
    )


# ─────────────────────────────────────────────────────────────────────
#  UPDATE BUDGET
# ─────────────────────────────────────────────────────────────────────

async def _update_budget(sender: str, intent: dict, user: dict, send_message):
    user_id  = user["_id"]
    slug     = intent.get("pocket")
    amount   = intent.get("amount")
    currency = user.get("base_currency", "PKR")

    if not slug:
        await send_message(sender,
            "Which pocket's budget should I update?\n"
            "_e.g. change food budget to 3000_")
        return

    if not amount:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action": "awaiting_budget_amount",
                "pocket_slug": slug
            }}}
        )
        await send_message(sender,
            f"What should the new budget for *{slug}* be?\n"
            f"_e.g. 5000_")
        return

    result = await pockets_col.find_one_and_update(
        {"user_id": user_id, "slug": slug, "is_active": True},
        {"$set": {"allocated_budget": amount}},
        return_document=True
    )

    if not result:
        await send_message(sender,
            f"Couldn't find a *{slug}* pocket.\n"
            "Reply *balance* to see your pockets.")
        return

    await send_message(sender,
        f"✅ *{result['name']}* budget updated to "
        f"*{amount:.0f} {currency}*\n"
        f"Current balance: *{result['current_balance']:.0f} {currency}*"
    )


# ─────────────────────────────────────────────────────────────────────
#  RENAME POCKET
# ─────────────────────────────────────────────────────────────────────

async def _rename_pocket(sender: str, intent: dict, user: dict, send_message):
    user_id  = user["_id"]
    old_slug = intent.get("pocket")
    new_name = intent.get("merchant")

    if not old_slug or not new_name:
        await send_message(sender,
            "Please specify old and new name.\n"
            "_e.g. rename food to groceries_")
        return

    new_slug = new_name.lower().strip().replace(" ", "-")

    result = await pockets_col.find_one_and_update(
        {"user_id": user_id, "slug": old_slug, "is_active": True},
        {"$set": {"name": new_name.title(), "slug": new_slug}},
        return_document=True
    )

    if not result:
        await send_message(sender,
            f"Couldn't find a *{old_slug}* pocket.")
        return

    await send_message(sender,
        f"✅ Renamed *{old_slug.title()}* → *{new_name.title()}*"
    )


# ─────────────────────────────────────────────────────────────────────
#  DELETE POCKET
# ─────────────────────────────────────────────────────────────────────

async def _delete_pocket(sender: str, intent: dict, user: dict, send_message):
    user_id  = user["_id"]
    slug     = intent.get("pocket")
    currency = user.get("base_currency", "PKR")

    if not slug:
        await send_message(sender,
            "Which pocket should I delete?\n"
            "_e.g. delete travel pocket_")
        return

    result = await pockets_col.find_one_and_update(
        {"user_id": user_id, "slug": slug, "is_active": True},
        {"$set": {"is_active": False}},
        return_document=True
    )

    if not result:
        await send_message(sender,
            f"Couldn't find a *{slug}* pocket.")
        return

    await send_message(sender,
        f"🗑️ *{result['name']}* pocket deleted.\n"
        f"It had *{result['current_balance']:.0f} {currency}* remaining."
    )


# ─────────────────────────────────────────────────────────────────────
#  ADD INCOME
# ─────────────────────────────────────────────────────────────────────

async def _add_income(sender: str, intent: dict, user: dict, send_message):
    amount   = intent.get("amount")
    currency = user.get("base_currency", "PKR")

    if not amount:
        await send_message(sender,
            "Please specify your income.\n"
            "_e.g. my salary is 150000_")
        return

    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"financial_profile.monthly_income": amount}}
    )

    await send_message(sender,
        f"✅ Income updated to *{amount:.0f} {currency}/month*\n\n"
        f"📊 *50/30/20 breakdown:*\n"
        f"Needs (50%):   {amount * 0.50:.0f} {currency}\n"
        f"Wants (30%):   {amount * 0.30:.0f} {currency}\n"
        f"Savings (20%): {amount * 0.20:.0f} {currency}\n\n"
        "_Reply *how am I doing* to see if your pockets match this._"
    )


# ─────────────────────────────────────────────────────────────────────
#  SET ADVISOR MODE
# ─────────────────────────────────────────────────────────────────────

async def _set_advisor_mode(sender: str, intent: dict, user: dict, send_message):
    mode = intent.get("merchant")

    if mode not in ("off", "proactive", "on_request"):
        await send_message(sender,
            "Please specify:\n"
            "• *turn off advice*\n"
            "• *turn on advisor* (proactive alerts)\n"
            "• *only advise when I ask*")
        return

    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"settings.advisor_mode": mode}}
    )

    labels = {
        "off":        "🔕 Advisor is now *off*. I'll stay quiet.",
        "proactive":  "🔔 I'll alert you when you're close to overspending.",
        "on_request": "💬 I'll only advise when you ask."
    }
    await send_message(sender, labels[mode])