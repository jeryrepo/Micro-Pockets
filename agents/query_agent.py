"""
query_agent.py
──────────────
Handles all READ operations for Micro-Pockets.

Supported intents:
  check_balance    → show pocket balances
  monthly_summary  → full month overview with savings target
  spending_query   → flexible transaction/spending queries via query object
  query_income     → show stored income
  request_advice   → financial advice via advisor agent
  stock_query      → live stock prices via stock agent
  greeting/help    → show what the bot can do
  unknown          → fallback to mcp_query_agent for natural language
"""

from datetime import datetime, timezone, timedelta
from bson import ObjectId
from core.database import pockets_col, transactions_col, users_col


# ─────────────────────────────────────────────────────────────────────
#  TIME RANGE → MongoDB filter
# ─────────────────────────────────────────────────────────────────────

def _time_filter(time_range: str) -> dict:
    now = datetime.now(timezone.utc)

    if time_range == "today":
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        return {"$gte": start}
    elif time_range == "this_week":
        start = now - timedelta(days=now.weekday())
        start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        return {"$gte": start}
    elif time_range == "this_month":
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return {"$gte": start}
    elif time_range == "last_month":
        if now.month == 1:
            start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
            end   = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        else:
            start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
            end   = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return {"$gte": start, "$lt": end}
    else:
        return {}


# ─────────────────────────────────────────────────────────────────────
#  POCKET RESOLVER
# ─────────────────────────────────────────────────────────────────────

async def _resolve_pocket(user_id, pocket_filter: str):
    if not pocket_filter:
        return None
    return await pockets_col.find_one({
        "user_id":   user_id,
        "is_active": True,
        "$or": [
            {"slug": {"$regex": pocket_filter, "$options": "i"}},
            {"name": {"$regex": pocket_filter, "$options": "i"}}
        ]
    })


# ─────────────────────────────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────────────────────────────

async def handle(
    sender:       str,
    intent:       dict,
    user:         dict,
    send_message
):
    i        = intent.get("intent")
    currency = user.get("base_currency", "PKR")
    user_id  = user["_id"]

    if i == "check_balance":
        await _query_balance(sender, intent, user, send_message)

    elif i == "monthly_summary":
        await _monthly_summary(sender, user, send_message)

    elif i == "spending_query":
        await _spending_query(sender, intent, user, send_message)

    elif i == "query_income":
        await _query_income(sender, user, send_message)

    elif i == "request_advice":
        from agents.advisor_agent import advise_spending
        await advise_spending(sender, user, send_message)

    elif i == "stock_query":
        from agents.stock_agent import handle as stock_handle
        await stock_handle(sender, intent, user, send_message)

    elif i in ("greeting", "help"):
        await _help(sender, user, send_message)

    else:
        # Fallback to natural language MCP query agent
        from agents.mcp_query_agent import handle as mcp_handle
        await mcp_handle(
            sender, intent.get("raw", ""), user,
            intent.get("language", "en"),
            intent.get("language_name", "English"),
            send_message
        )


# ─────────────────────────────────────────────────────────────────────
#  CHECK BALANCE
# ─────────────────────────────────────────────────────────────────────

async def _query_balance(sender, intent, user, send_message):
    currency   = user.get("base_currency", "PKR")
    user_id    = user["_id"]
    pocket_slug = intent.get("pocket")

    if pocket_slug:
        pocket = await pockets_col.find_one({
            "user_id": user_id, "is_active": True,
            "$or": [{"slug": pocket_slug}, {"name": {"$regex": pocket_slug, "$options": "i"}}]
        })
        if not pocket:
            await send_message(sender, f"Pocket *{pocket_slug}* not found.\nType _balance_ to see all pockets.")
            return
        pct   = round((pocket["current_balance"] / pocket["allocated_budget"]) * 100) if pocket.get("allocated_budget") else 0
        icon  = "🔴" if pct < 20 else ("🟡" if pct < 50 else "🟢")
        await send_message(sender,
            f"{icon} *{pocket['name']}*\n"
            f"{pocket['current_balance']:.0f}/{pocket['allocated_budget']:.0f} {currency} — {pct}% left"
        )
        return

    pockets = await pockets_col.find({"user_id": user_id, "is_active": True}).to_list(20)
    if not pockets:
        await send_message(sender, "No pockets yet.\nCreate one: _create pocket food 5000_")
        return

    lines = ["📊 *Your Pockets*\n"]
    for p in pockets:
        bal    = p.get("current_balance", 0)
        budget = p.get("allocated_budget", 0)
        pct    = round((bal / budget) * 100) if budget else 0
        icon   = "🔴" if pct < 20 else ("🟡" if pct < 50 else "🟢")
        lines.append(f"{icon} *{p['name']}*: {bal:.0f}/{budget:.0f} {currency} — {pct}% left")

    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  MONTHLY SUMMARY
# ─────────────────────────────────────────────────────────────────────

async def _monthly_summary(sender, user, send_message):
    currency = user.get("base_currency", "PKR")
    user_id  = user["_id"]
    name     = user.get("name", "")
    income   = user.get("financial_profile", {}).get("monthly_income", 0)
    now      = datetime.now(timezone.utc)
    month    = now.strftime("%B %Y")

    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    pockets     = await pockets_col.find({"user_id": user_id, "is_active": True}).to_list(20)

    lines = [f"📊 *{name}'s {month} Summary*\n"]
    total_spent  = 0
    total_budget = 0

    for p in pockets:
        bal    = p.get("current_balance", 0)
        budget = p.get("allocated_budget", 0)
        spent  = budget - bal
        pct    = round((bal / budget) * 100) if budget else 0
        icon   = "🔴" if pct < 20 else ("🟡" if pct < 50 else "🟢")
        total_spent  += spent
        total_budget += budget
        lines.append(f"{icon} *{p['name']}*: {spent:.0f}/{budget:.0f} {currency} — {pct}% left")

    lines.append(f"\n💸 *Total spent: {total_spent:.0f} {currency}*")

    if income:
        savings_target = income * 0.20
        lines.append(f"🎯 Savings target (20%): {savings_target:.0f} {currency}/month")

    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  SPENDING QUERY
# ─────────────────────────────────────────────────────────────────────

async def _spending_query(sender, intent, user, send_message):
    currency = user.get("base_currency", "PKR")
    user_id  = user["_id"]
    q        = intent.get("query") or {}

    query_type      = q.get("type", "transaction_list")
    time_range      = q.get("time_range", "this_month")
    pocket_filter   = q.get("pocket_filter")
    merchant_filter = q.get("merchant_filter")
    limit           = int(q.get("limit") or 10)
    sort_by         = q.get("sort", "recent")
    detail_level    = q.get("detail_level", "itemized")

    # Build base match
    match = {"user_id": user_id, "status": "confirmed"}
    time_f = _time_filter(time_range)
    if time_f:
        match["timestamp"] = time_f

    # Pocket filter
    if pocket_filter:
        pocket = await _resolve_pocket(user_id, pocket_filter)
        if not pocket:
            await send_message(sender,
                f"Pocket *{pocket_filter}* not found.\n"
                "Type _balance_ to see your pockets.")
            return
        match["pocket_id"] = pocket["_id"]

    # Merchant filter
    if merchant_filter:
        match["merchant"] = {"$regex": merchant_filter, "$options": "i"}

    # Dispatch
    if query_type == "pocket_summary" or detail_level == "aggregate":
        await _aggregate_by_pocket(sender, match, currency, time_range, send_message)
    else:
        sort_field = "amount_base" if sort_by == "highest_amount" else "timestamp"
        label_map  = {
            "transaction_list": "Transactions",
            "top_spending":     "Top Expenses",
            "merchant_search":  f"{merchant_filter} Transactions" if merchant_filter else "Transactions",
            "single_pocket":    f"{pocket_filter} Transactions" if pocket_filter else "Transactions",
        }
        label = label_map.get(query_type, "Transactions")
        await _list_transactions(sender, match, limit, sort_field, currency, time_range, label, send_message)


async def _list_transactions(sender, match, limit, sort_field, currency, time_range, label, send_message):
    txns = await transactions_col.find(match).sort(sort_field, -1).limit(limit).to_list(limit)

    if not txns:
        period = time_range.replace("_", " ")
        await send_message(sender, f"No transactions found for {period}.")
        return

    pocket_ids = list({t["pocket_id"] for t in txns if t.get("pocket_id")})
    pockets    = await pockets_col.find({"_id": {"$in": pocket_ids}}).to_list(50)
    pocket_map = {p["_id"]: p["name"] for p in pockets}
    total      = sum(t.get("amount_base", 0) for t in txns)
    period     = time_range.replace("_", " ").title()

    lines = [f"🧾 *{label} — {period}*\n"]
    for t in txns:
        date        = t["timestamp"].strftime("%b %d") if t.get("timestamp") else "?"
        merchant    = t.get("merchant") or "Unknown"
        amount      = t.get("amount_base", 0)
        pocket_name = pocket_map.get(t.get("pocket_id"), "Uncategorised")
        lines.append(f"• {currency} {amount:,.0f} at *{merchant}* → _{pocket_name}_ ({date})")

    lines.append(f"\n💰 *Total: {currency} {total:,.0f}*")
    await send_message(sender, "\n".join(lines))


async def _aggregate_by_pocket(sender, match, currency, time_range, send_message):
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id":         "$pocket_id",
            "total_spent": {"$sum": "$amount_base"},
            "count":       {"$sum": 1}
        }},
        {"$lookup": {
            "from": "pockets", "localField": "_id",
            "foreignField": "_id", "as": "pocket"
        }},
        {"$unwind": {"path": "$pocket", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "pocket_name":      {"$ifNull": ["$pocket.name", "Unknown"]},
            "allocated_budget": "$pocket.allocated_budget",
            "total_spent":      1,
            "count":            1,
        }},
        {"$sort": {"total_spent": -1}}
    ]

    rows = await transactions_col.aggregate(pipeline).to_list(50)
    if not rows:
        period = time_range.replace("_", " ")
        await send_message(sender, f"No transactions found for {period}.")
        return

    period      = time_range.replace("_", " ").title()
    grand_total = sum(r["total_spent"] for r in rows)
    lines       = [f"📊 *Spending Breakdown — {period}*\n"]

    for r in rows:
        budget = r.get("allocated_budget") or 0
        pct    = round((r["total_spent"] / budget) * 100) if budget else 0
        icon   = "🔴" if pct >= 90 else ("🟡" if pct >= 70 else "🟢")
        count  = r["count"]
        lines.append(
            f"{icon} *{r['pocket_name']}*: {currency} {r['total_spent']:,.0f} "
            f"({count} txn{'s' if count > 1 else ''})"
        )

    lines.append(f"\n💰 *Total: {currency} {grand_total:,.0f}*")
    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  QUERY INCOME
# ─────────────────────────────────────────────────────────────────────

async def _query_income(sender, user, send_message):
    currency = user.get("base_currency", "PKR")
    income   = user.get("financial_profile", {}).get("monthly_income", 0)
    if not income:
        await send_message(sender,
            "No income set yet.\n"
            "Update it: _my salary is 150000_")
        return
    needs   = income * 0.50
    wants   = income * 0.30
    savings = income * 0.20
    await send_message(sender,
        f"💰 *Your Income*\n\n"
        f"Monthly: *{income:,.0f} {currency}*\n\n"
        f"50/30/20 breakdown:\n"
        f"🏠 Needs (50%): {needs:,.0f} {currency}\n"
        f"🎯 Wants (30%): {wants:,.0f} {currency}\n"
        f"💎 Savings (20%): {savings:,.0f} {currency}"
    )


# ─────────────────────────────────────────────────────────────────────
#  HELP
# ─────────────────────────────────────────────────────────────────────

async def _help(sender, user, send_message):
    name = user.get("name", "")
    await send_message(sender,
        f"👋 *Hey {name}! Here is what I can do:*\n\n"
        f"💰 *Log expense*\n"
        f"  _spent 500 on food_\n"
        f"  _uber 450_\n"
        f"  _1k on groceries_\n\n"
        f"📊 *Check balance*\n"
        f"  _balance_\n"
        f"  _how much left in food_\n\n"
        f"🧾 *Transactions*\n"
        f"  _what did I spend today_\n"
        f"  _show last 5 transactions_\n"
        f"  _biggest expenses this week_\n\n"
        f"📈 *Monthly summary*\n"
        f"  _how am I doing this month_\n\n"
        f"🤖 *Advice*\n"
        f"  _advice me_\n"
        f"  _how should I spend my money_\n\n"
        f"➕ *Manage pockets*\n"
        f"  _create pocket travel 5000_\n"
        f"  _change food budget to 3000_\n\n"
        f"📉 *Stocks*\n"
        f"  _Apple stock price_\n"
        f"  _OGDC.KA summary_"
    )