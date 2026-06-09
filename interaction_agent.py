"""
interaction_agent.py
────────────────────
Interaction Agent — handles all manual user CRUD operations.

Receives structured intent from interpreter_agent.py
Reads/writes MongoDB via mcp_tools.py
Sends WhatsApp replies via send_whatsapp_message()

Handles:
  add_expense, delete_expense, query_balance, query_transactions,
  monthly_summary, create_pocket, update_budget, rename_pocket,
  delete_pocket, add_income, set_advisor_mode, confirm, cancel, help
"""

from database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone


async def handle(
    sender: str,
    intent: dict,
    user: dict,
    send_message   # callable — send_whatsapp_message from main.py
):
    """
    Main entry point. Routes to the correct handler based on intent.
    """
    i = intent["intent"]

    if   i == "add_expense":        await _add_expense(sender, intent, user, send_message)
    elif i == "delete_expense":     await _delete_expense(sender, user, send_message)
    elif i == "query_balance":      await _query_balance(sender, intent, user, send_message)
    elif i == "query_transactions": await _query_transactions(sender, intent, user, send_message)
    elif i == "monthly_summary":    await _monthly_summary(sender, user, send_message)
    elif i == "create_pocket":      await _create_pocket(sender, intent, user, send_message)
    elif i == "update_budget":      await _update_budget(sender, intent, user, send_message)
    elif i == "rename_pocket":      await _rename_pocket(sender, intent, user, send_message)
    elif i == "delete_pocket":      await _delete_pocket(sender, intent, user, send_message)
    elif i == "add_income":         await _add_income(sender, intent, user, send_message)
    elif i == "set_advisor_mode":   await _set_advisor_mode(sender, intent, user, send_message)
    elif i == "confirm":            await _confirm(sender, user, send_message)
    elif i == "cancel":             await _cancel(sender, user, send_message)
    else:                           await _help(sender, send_message)


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

    # Try to find the pocket by slug
    pocket = None
    if slug:
        pocket = await pockets_col.find_one({
            "user_id": user_id, "slug": slug, "is_active": True
        })

    # Pocket not found — ask user which one
    if not pocket:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)

        pocket_list = "\n".join([
            f"• *{p['name']}* — {p['current_balance']:.0f} {currency} left"
            for p in pockets
        ])
        await send_message(sender,
            f"Which pocket for *{amount:.0f} {currency}*?\n\n"
            f"{pocket_list}\n\n_Reply with the pocket name_"
        )
        # Store pending intent so confirm step knows what to do
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

    # Insert transaction as pending_review
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

    # Save pending txn id for confirm step
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
        await send_message(sender,
            "Nothing pending to confirm. What would you like to do?")
        return

    txn = await transactions_col.find_one({"_id": ObjectId(txn_id)})
    if not txn or txn["status"] != "pending_review":
        await send_message(sender, "Transaction already processed.")
        return

    # Confirm transaction
    await transactions_col.update_one(
        {"_id": ObjectId(txn_id)},
        {"$set": {"status": "confirmed"}}
    )

    # Atomic balance deduction
    updated_pocket = await pockets_col.find_one_and_update(
        {"_id": txn["pocket_id"]},
        {"$inc": {"current_balance": -txn["amount_base"]}},
        return_document=True
    )

    # Clear pending state
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

    # Let advisor check if alert needed
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

    # Restore balance atomically
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
#  QUERY BALANCE
# ─────────────────────────────────────────────────────────────────────

async def _query_balance(sender: str, intent: dict, user: dict, send_message):
    user_id  = user["_id"]
    currency = user.get("base_currency", "PKR")
    slug     = intent.get("pocket")

    if slug:
        pocket = await pockets_col.find_one({
            "user_id": user_id, "slug": slug, "is_active": True
        })
        if not pocket:
            await send_message(sender,
                f"I couldn't find a *{slug}* pocket.\n"
                "Reply *balance* to see all your pockets.")
            return

        allocated = pocket["allocated_budget"]
        spent     = allocated - pocket["current_balance"]
        pct_used  = (spent / allocated * 100) if allocated else 0
        pct_left  = max(0, 100 - pct_used)
        icon      = "🔴" if pct_used >= 100 else "🔴" if pct_used >= 90 else "🟡" if pct_used >= 70 else "🟢"

        if pocket["current_balance"] < 0:
            over_by = abs(pocket["current_balance"])
            income  = user.get("financial_profile", {}).get("monthly_income", 0)
            name    = user.get("name", "")
            warning = ""
            if income and spent > income:
                warning = f"\n\n🚨 *Whoa{' ' + name if name else ''}!* You've spent more than your monthly income on this pocket!"
            status_line = f"🔴 Over budget by *{over_by:.0f} {currency}*{warning}"
        else:
            status_line = f"Remaining: *{pocket['current_balance']:.0f} {currency}* ({pct_left:.0f}% left)"

        await send_message(sender,
            f"{icon} *{pocket['name']}*\n"
            f"Budget: {allocated:.0f} {currency}\n"
            f"Spent:  {max(spent, 0):.0f} {currency} ({pct_used:.0f}% used)\n"
            f"{status_line}"
        )
    else:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)

        if not pockets:
            await send_message(sender, "You have no active pockets yet.")
            return

        income = user.get("financial_profile", {}).get("monthly_income", 0)
        name   = user.get("name", "")
        lines  = ["📊 *Your Pockets*\n"]

        total_spent = 0
        for p in pockets:
            allocated = p["allocated_budget"]
            spent_p   = allocated - p["current_balance"]
            pct_used  = (spent_p / allocated * 100) if allocated else 0
            pct_left  = max(0, 100 - pct_used)
            total_spent += max(spent_p, 0)

            if p["current_balance"] < 0:
                over_by = abs(p["current_balance"])
                status  = f"🔴 over by {over_by:.0f}"
            else:
                icon   = "🔴" if pct_used >= 90 else "🟡" if pct_used >= 70 else "🟢"
                status = f"{icon} {pct_left:.0f}% left"

            lines.append(
                f"*{p['name']}*: {max(spent_p,0):.0f}/{allocated:.0f} {currency} — {status}"
            )

        # Warn if total spending exceeds income
        if income and total_spent > income:
            lines.append(
                f"\n🚨 *{'Hey ' + name + '!' if name else 'Warning!'}* "
                f"Total spending ({total_spent:.0f}) exceeds your monthly income ({income:.0f} {currency})!"
            )

        await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  QUERY TRANSACTIONS
# ─────────────────────────────────────────────────────────────────────

async def _query_transactions(sender: str, intent: dict, user: dict, send_message):
    user_id  = user["_id"]
    currency = user.get("base_currency", "PKR")
    slug     = intent.get("pocket")

    query = {"user_id": user_id, "status": "confirmed"}
    if slug:
        pocket = await pockets_col.find_one(
            {"user_id": user_id, "slug": slug, "is_active": True}
        )
        if pocket:
            query["pocket_id"] = pocket["_id"]

    txns = await transactions_col.find(query).sort(
        "timestamp", -1
    ).to_list(10)

    if not txns:
        await send_message(sender,
            "No transactions found yet.\n"
            "Start logging: _spent 500 on food_")
        return

    lines = ["🧾 *Recent Transactions*\n"]
    for t in txns:
        date = t["timestamp"].strftime("%d %b")
        lines.append(
            f"• {date} — *{t['amount_base']:.0f} {currency}* "
            f"({t.get('merchant', 'expense')})"
        )
    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  MONTHLY SUMMARY
# ─────────────────────────────────────────────────────────────────────

async def _monthly_summary(sender: str, user: dict, send_message):
    from mcp_tools import aggregate_monthly_trends

    user_id  = str(user["_id"])
    currency = user.get("base_currency", "PKR")
    trends   = await aggregate_monthly_trends(user_id)

    if not trends:
        await send_message(sender,
            "No confirmed transactions this month yet.\n"
            "Start logging: _spent 500 on food_")
        return

    now         = datetime.now(timezone.utc)
    name        = user.get("name", "")
    month_label = now.strftime('%B %Y')
    summary_title = f"{name}'s {month_label} Summary" if name else f"{month_label} Summary"
    lines       = [f"📈 *{summary_title}*\n"]
    total_spent = 0

    for t in trends:
        pct_used = t.get("pct_used", 0)
        pct_left = max(0, 100 - pct_used)
        icon     = "🔴" if pct_used >= 100 else "🔴" if pct_used >= 90 else "🟡" if pct_used >= 70 else "🟢"

        if t["total_spent"] > t["allocated_budget"]:
            over_by    = t["total_spent"] - t["allocated_budget"]
            status_str = f"over by {over_by:.0f} 🔴"
        else:
            status_str = f"{pct_left:.0f}% left"

        lines.append(
            f"{icon} *{t['pocket_name']}*: "
            f"{t['total_spent']:.0f}/{t['allocated_budget']:.0f} "
            f"{currency} — {status_str}"
        )
        total_spent += t["total_spent"]

    lines.append(f"\n💸 *Total spent: {total_spent:.0f} {currency}*")

    income = user.get("financial_profile", {}).get("monthly_income")
    if income:
        lines.append(
            f"🎯 Savings target (20%): "
            f"{income * 0.20:.0f} {currency}/month"
        )
        if total_spent > income:
            name = user.get("name", "")
            lines.append(
                f"\n🚨 *{'Hey ' + name + '!' if name else 'Warning!'} "
                f"You've spent more than your monthly income this month!*"
            )

    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  CREATE POCKET
# ─────────────────────────────────────────────────────────────────────

async def _create_pocket(sender: str, intent: dict, user: dict, send_message):
    from database import users_col
    user_id  = str(user["_id"])
    slug     = intent.get("pocket") or ""
    name     = slug.replace("-", " ").title() if slug else ""
    budget   = intent.get("amount")
    currency = user.get("base_currency", "PKR")

    # No pocket name provided — ask for it
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

    # Pocket name provided but no budget — ask for it
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
        await send_message(sender,
            f"What should the new budget for *{slug}* be?\n"
            f"_e.g. change {slug} budget to 3000_")
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
            "• *alert me when I overspend*\n"
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


# ─────────────────────────────────────────────────────────────────────
#  HELP
# ─────────────────────────────────────────────────────────────────────

async def _help(sender: str, send_message):
    from database import users_col
    user = await users_col.find_one({"whatsapp_number": sender})
    name = user.get("name", "") if user else ""

    await send_message(sender,
        f"👋 *{'Hey ' + name + '!' if name else 'Hey!'} Here's what I can do:*\n\n"
        "💰 *Log expense*\n"
        "   _spent 500 on food_\n"
        "   _uber 450_\n\n"
        "📊 *Check balance*\n"
        "   _how much left in food_\n"
        "   _balance_\n\n"
        "🧾 *Transactions*\n"
        "   _what did I spend today_\n"
        "   _show food transactions_\n\n"
        "📈 *Monthly summary*\n"
        "   _how am I doing this month_\n\n"
        "➕ *Manage pockets*\n"
        "   _create pocket travel 5000_\n"
        "   _change food budget to 3000_\n"
        "   _rename food to groceries_\n"
        "   _delete travel pocket_\n\n"
        "💵 *Update income*\n"
        "   _my salary is 150000_\n\n"
        "⚙️  *Advisor settings*\n"
        "   _turn off advice_\n"
        "   _alert me when I overspend_"
    )