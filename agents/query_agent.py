"""
query_agent.py
──────────────
Query Agent — handles all READ operations.

Responsibilities:
  query_balance      → show pocket balances
  query_transactions → show recent transaction history
  monthly_summary    → full month spending overview
  query_income       → show stored income + 50/30/20 breakdown
  request_advice     → financial advice based on spending data
  help               → show what the bot can do

Never writes to MongoDB. Never modifies any document.
All reads go through mcp_tools.py aggregations or direct collection queries.
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
    """Main entry point — routes to correct read handler."""
    i = intent["intent"]

    if   i == "query_balance":      await _query_balance(sender, intent, user, send_message)
    elif i == "query_transactions":  await _query_transactions(sender, intent, user, send_message)
    elif i == "monthly_summary":     await _monthly_summary(sender, user, send_message)
    elif i == "query_income":        await _query_income(sender, user, send_message)
    elif i == "stock_query":
        from agents.stock_agent import handle as stock_handle
        await stock_handle(sender, intent, user, send_message)
    elif i == "request_advice":
        raw = (intent.get("raw") or "").lower()

        # "What is advisor" type questions → always explain, don't give spending analysis
        explain_keywords = [
            "what is", "what's", "explain", "how does", "how do",
            "tell me about", "what does advisor", "advisor agent"
        ]
        income_keywords = [
            "salary", "income", "earn", "spend my",
            "how should i", "how to spend", "manage money",
            "budget advice", "financial advice", "investment"
        ]

        if any(kw in raw for kw in explain_keywords):
            # Always explain what advisor is
            mode = user.get("settings", {}).get("advisor_mode", "off")
            mode_label = {
                "off":        "Off 🔕",
                "proactive":  "Proactive 🔔",
                "on_request": "On request 💬"
            }.get(mode, "Off 🔕")
            name = user.get("name", "")
            await send_message(sender,
                f"🤖 *Advisor Agent*\n\n"
                f"I analyse your spending and help you stay on budget.\n\n"
                f"*What I do:*\n"
                f"• Alert you when a pocket is almost full\n"
                f"• Give 50/30/20 budget breakdowns\n"
                f"• Suggest where to cut back\n"
                f"• Check if you're on track for savings\n\n"
                f"*Current mode:* {mode_label}\n\n"
                f"*Change mode:*\n"
                f"• _turn on advisor_ — alert me proactively\n"
                f"• _only advise when I ask_ — on demand\n"
                f"• _turn off advice_ — stay silent\n\n"
                f"_Try: advice me how to spend my salary_"
            )
        elif any(kw in raw for kw in income_keywords):
            from agents.advisor_agent import advise_spending
            await advise_spending(sender, user, send_message)
        else:
            await _request_advice(sender, user, send_message)
    else:
        await _help(sender, user, send_message)


# ─────────────────────────────────────────────────────────────────────
#  QUERY BALANCE
# ─────────────────────────────────────────────────────────────────────

async def _query_balance(sender: str, intent: dict, user: dict, send_message):
    """Show balance for one pocket or all pockets."""
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
        icon      = "🔴" if pct_used >= 90 else "🟡" if pct_used >= 70 else "🟢"

        if pocket["current_balance"] < 0:
            over_by = abs(pocket["current_balance"])
            income  = user.get("financial_profile", {}).get("monthly_income", 0)
            name    = user.get("name", "")
            warning = ""
            if income and spent > income:
                warning = (
                    f"\n\n🚨 *Whoa{' ' + name if name else ''}!* "
                    f"You've spent more than your monthly income on this pocket!"
                )
            status_line = f"🔴 Over budget by *{over_by:.0f} {currency}*{warning}"
        else:
            status_line = (
                f"Remaining: *{pocket['current_balance']:.0f} {currency}* "
                f"({pct_left:.0f}% left)"
            )

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
            await send_message(sender,
                "You have no active pockets yet.\n"
                "_Create one: create pocket food 5000_")
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
                f"*{p['name']}*: "
                f"{max(spent_p, 0):.0f}/{allocated:.0f} {currency} — {status}"
            )

        if income and total_spent > income:
            lines.append(
                f"\n🚨 *{'Hey ' + name + '!' if name else 'Warning!'}* "
                f"Total spending exceeds your monthly income!"
            )

        await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  QUERY TRANSACTIONS
# ─────────────────────────────────────────────────────────────────────

async def _query_transactions(sender: str, intent: dict, user: dict, send_message):
    """Show last 10 confirmed transactions, optionally filtered by pocket."""
    user_id  = user["_id"]
    currency = user.get("base_currency", "PKR")
    slug     = intent.get("pocket")

    query = {"user_id": user_id, "status": "confirmed"}

    if slug:
        pocket = await pockets_col.find_one({
            "user_id": user_id, "slug": slug, "is_active": True
        })
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
    """Full month spending overview with savings check."""
    from core.mcp_tools import aggregate_monthly_trends

    user_id  = str(user["_id"])
    currency = user.get("base_currency", "PKR")
    name     = user.get("name", "")
    trends   = await aggregate_monthly_trends(user_id)

    if not trends:
        await send_message(sender,
            "No confirmed transactions this month yet.\n"
            "Start logging: _spent 500 on food_")
        return

    now           = datetime.now(timezone.utc)
    month_label   = now.strftime("%B %Y")
    summary_title = f"{name}'s {month_label} Summary" if name else f"{month_label} Summary"
    lines         = [f"📈 *{summary_title}*\n"]
    total_spent   = 0

    for t in trends:
        pct_used = t.get("pct_used", 0)
        pct_left = max(0, 100 - pct_used)
        icon     = "🔴" if pct_used >= 90 else "🟡" if pct_used >= 70 else "🟢"

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
        savings_target = income * 0.20
        lines.append(f"🎯 Savings target (20%): {savings_target:.0f} {currency}/month")
        if total_spent > income:
            lines.append(
                f"\n🚨 *{'Hey ' + name + '!' if name else 'Warning!'} "
                f"You've spent more than your monthly income this month!*"
            )

    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  QUERY INCOME
# ─────────────────────────────────────────────────────────────────────

async def _query_income(sender: str, user: dict, send_message):
    """Show stored income with 50/30/20 breakdown."""
    currency    = user.get("base_currency", "PKR")
    name        = user.get("name", "")
    income      = user.get("financial_profile", {}).get("monthly_income")
    income_label = f"{name}'s Income" if name else "Your Income"

    if not income:
        await send_message(sender,
            "I don't have your income stored yet.\n"
            "Tell me: _my salary is 150000_")
        return

    await send_message(sender,
        f"💵 *{income_label}*\n\n"
        f"Monthly: *{income:.0f} {currency}*\n\n"
        f"📊 *50/30/20 breakdown:*\n"
        f"Needs (50%):   {income * 0.50:.0f} {currency}\n"
        f"Wants (30%):   {income * 0.30:.0f} {currency}\n"
        f"Savings (20%): {income * 0.20:.0f} {currency}\n\n"
        "_To update: my salary is 200000_"
    )


# ─────────────────────────────────────────────────────────────────────
#  REQUEST ADVICE
# ─────────────────────────────────────────────────────────────────────

async def _request_advice(sender: str, user: dict, send_message):
    """
    Give financial advice based on current spending.
    On_request path — fires when user explicitly asks.
    Also explains what the advisor is if user asks.
    """
    from core.mcp_tools import aggregate_monthly_trends

    currency = user.get("base_currency", "PKR")
    name     = user.get("name", "")
    income   = user.get("financial_profile", {}).get("monthly_income", 0)
    mode     = user.get("settings", {}).get("advisor_mode", "off")
    user_id  = str(user["_id"])
    trends   = await aggregate_monthly_trends(user_id)

    mode_label = {
        "off":        "Off 🔕",
        "proactive":  "Proactive 🔔",
        "on_request": "On request 💬"
    }.get(mode, "Off 🔕")

    # No spending data yet — explain what advisor does
    if not trends:
        await send_message(sender,
            f"🤖 *Advisor Agent*\n\n"
            "I analyse your spending patterns and alert you when "
            "you're close to going over budget.\n\n"
            f"*Current mode:* {mode_label}\n\n"
            "Start logging expenses and I'll give you "
            "personalised advice.\n\n"
            "To change mode:\n"
            "• _turn on advisor_ — proactive alerts\n"
            "• _only advise when I ask_ — on demand\n"
            "• _turn off advice_ — silent mode"
        )
        return

    # Real advice from spending data
    lines    = [f"🤖 *{'Hey ' + name + '! ' if name else ''}Here is my advice:*\n"]
    concerns = []
    healthy  = []

    for t in trends:
        pct_used = t.get("pct_used", 0)
        if pct_used >= 90:
            concerns.append(
                f"⚠️ *{t['pocket_name']}* is at {pct_used:.0f}% — "
                f"only {max(t.get('remaining', 0), 0):.0f} {currency} left"
            )
        elif pct_used <= 50:
            healthy.append(
                f"✅ *{t['pocket_name']}* is healthy ({pct_used:.0f}% used)"
            )

    if concerns:
        lines.append("*Watch out:*")
        lines.extend(concerns)
        lines.append("")

    if healthy:
        lines.append("*Looking good:*")
        lines.extend(healthy)
        lines.append("")

    if income:
        total_spent    = sum(t["total_spent"] for t in trends)
        savings_so_far = income - total_spent
        on_track       = savings_so_far >= income * 0.20
        lines.append(
            f"💰 *Savings check:*\n"
            f"Target:   {income * 0.20:.0f} {currency}\n"
            f"On track: {'✅ Yes' if on_track else '❌ Not yet'}"
        )

    lines.append(f"\n_Mode: {mode_label}_")
    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  HELP
# ─────────────────────────────────────────────────────────────────────

async def _help(sender: str, user: dict, send_message):
    """Show full command reference."""
    name = user.get("name", "")

    await send_message(sender,
        f"👋 *{'Hey ' + name + '! ' if name else ''}Here is what I can do:*\n\n"
        "💰 *Log expense*\n"
        "   _spent 500 on food_\n"
        "   _uber 450_\n"
        "   _1k on groceries_\n\n"
        "📊 *Check balance*\n"
        "   _balance_\n"
        "   _how much left in food_\n\n"
        "🧾 *Transactions*\n"
        "   _what did I spend today_\n"
        "   _show food transactions_\n\n"
        "📈 *Monthly summary*\n"
        "   _how am I doing this month_\n\n"
        "💵 *Income*\n"
        "   _what's my salary_\n"
        "   _my salary is 150000_\n\n"
        "🤖 *Advice*\n"
        "   _advice me_\n"
        "   _how should I spend my income_\n\n"
        "➕ *Manage pockets*\n"
        "   _create pocket travel 5000_\n"
        "   _change food budget to 3000_\n"
        "   _rename food to groceries_\n"
        "   _delete travel pocket_\n\n"
        "⚙️  *Advisor settings*\n"
        "   _turn on advisor_\n"
        "   _turn off advice_\n"
        "   _only advise when I ask_\n\n"
        "🏦 *Bank alerts*\n"
        "   type *link* to get setup link\n"
        "   type *android* or *iphone* for guide"
    )