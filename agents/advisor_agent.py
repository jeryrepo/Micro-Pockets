"""
advisor_agent.py
────────────────
Advisor Agent — budget analysis, spending advice, proactive alerts.

Responsibilities:
  1. check_alert()     → fires after confirmed transaction if mode=proactive
  2. advise_spending() → called when user asks how to spend their income
                         gives personalised 50/30/20 + pocket recommendations

Never fires unsolicited unless advisor_mode = proactive AND threshold crossed.
Fires max once per pocket per month (alert_snoozed flag).
"""

from core.database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────
#  PROACTIVE ALERT — fires after confirmed transaction
# ─────────────────────────────────────────────────────────────────────

async def check_alert(
    sender: str,
    user: dict,
    pocket: dict,
    send_message
):
    """
    Called after every confirmed transaction.
    Only fires when:
      - advisor_mode == 'proactive'
      - pocket usage >= alert_threshold_pct
      - alert_snoozed == False
    """
    mode = user.get("settings", {}).get("advisor_mode", "off")
    if mode != "proactive":
        return

    if pocket.get("alert_snoozed"):
        return

    allocated = pocket.get("allocated_budget", 0)
    if not allocated:
        return

    balance   = pocket.get("current_balance", 0)
    spent     = allocated - balance
    pct_used  = (spent / allocated) * 100
    threshold = pocket.get("alert_threshold_pct", 80)

    if pct_used >= threshold:
        currency = user.get("base_currency", "PKR")
        name     = user.get("name", "")

        # Snooze — won't fire again this month for this pocket
        await pockets_col.update_one(
            {"_id": pocket["_id"]},
            {"$set": {"alert_snoozed": True}}
        )

        await send_message(sender,
            f"⚠️ *{pocket['name']}* is at *{pct_used:.0f}%* of budget\n"
            f"Spent: {spent:.0f} / {allocated:.0f} {currency}\n"
            f"Remaining: *{balance:.0f} {currency}*\n\n"
            f"_{'Hey ' + name + '! ' if name else ''}"
            f"Reply *advice me* for full spending analysis_"
        )


# ─────────────────────────────────────────────────────────────────────
#  SPENDING ADVICE — fired when user asks how to spend income
# ─────────────────────────────────────────────────────────────────────

async def advise_spending(
    sender: str,
    user: dict,
    send_message
):
    """
    Gives personalised spending advice based on:
    - Stored monthly income
    - Current pocket allocations vs 50/30/20 targets
    - Actual spending trends this month

    Called from query_agent when intent = request_advice
    and the user is asking about income management.
    """
    from core.mcp_tools import aggregate_monthly_trends

    currency = user.get("base_currency", "PKR")
    name     = user.get("name", "")
    income   = user.get("financial_profile", {}).get("monthly_income", 0)
    user_id  = str(user["_id"])

    if not income:
        await send_message(sender,
            "I need your income to give you advice.\n\n"
            "Tell me: _my salary is 150000_"
        )
        return

    # 50/30/20 targets
    needs_target    = income * 0.50
    wants_target    = income * 0.30
    savings_target  = income * 0.20

    # Get current pocket allocations
    pockets = await pockets_col.find(
        {"user_id": ObjectId(user_id), "is_active": True}
    ).to_list(20)

    total_allocated = sum(p["allocated_budget"] for p in pockets)
    unallocated     = income - total_allocated

    # Get actual spending this month
    trends      = await aggregate_monthly_trends(user_id)
    total_spent = sum(t["total_spent"] for t in trends) if trends else 0
    saved_so_far = income - total_spent

    greeting = f"Hey {name}! " if name else ""

    lines = [
        f"💡 *{greeting}Here's how to spend your {income:.0f} {currency}/month:*\n",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *50/30/20 Framework*\n",
        f"🏠 *Needs* (50%) — *{needs_target:.0f} {currency}*",
        f"   Bills, rent, food, transport",
        f"",
        f"🎯 *Wants* (30%) — *{wants_target:.0f} {currency}*",
        f"   Shopping, dining, entertainment",
        f"",
        f"💰 *Savings* (20%) — *{savings_target:.0f} {currency}*",
        f"   Emergency fund, investments",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Pocket allocation check
    if pockets:
        lines.append(f"\n📁 *Your current allocations:*")
        for p in pockets:
            pct_of_income = (p["allocated_budget"] / income * 100)
            lines.append(
                f"• *{p['name']}*: "
                f"{p['allocated_budget']:.0f} {currency} "
                f"({pct_of_income:.0f}% of income)"
            )

        if unallocated > 0:
            lines.append(
                f"\n💡 *{unallocated:.0f} {currency}* unallocated — "
                f"consider adding it to savings"
            )
        elif unallocated < 0:
            lines.append(
                f"\n⚠️ Your pockets exceed your income by "
                f"*{abs(unallocated):.0f} {currency}*"
            )

    # This month's actual spending
    if trends:
        lines.append(f"\n📈 *This month so far:*")
        lines.append(f"Spent:  {total_spent:.0f} {currency}")
        lines.append(f"Saved:  {max(saved_so_far, 0):.0f} {currency}")

        if saved_so_far < savings_target:
            shortfall = savings_target - saved_so_far
            lines.append(
                f"\n⚠️ You need *{shortfall:.0f} {currency}* more "
                f"to hit your savings target this month"
            )
        else:
            lines.append(f"✅ On track for savings goal!")

    # Investment suggestion
    lines.append(
        f"\n📈 *Tip:* Want to invest part of your savings?\n"
        f"Try: _show me stock AAPL_ or _show me stock OGDC.KA_\n"
        f"I can show you live stock prices to help you decide."
    )

    await send_message(sender, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  MONTHLY RESET — called by background scheduler on 1st of month
# ─────────────────────────────────────────────────────────────────────

async def monthly_reset(user_id: str) -> int:
    """Reset alert_snoozed on all pockets for a user."""
    result = await pockets_col.update_many(
        {"user_id": ObjectId(user_id), "is_active": True},
        {"$set": {
            "alert_snoozed":     False,
            "snooze_reset_date": datetime.now(timezone.utc)
        }}
    )
    print(f"✅ Advisor reset: {result.modified_count} pockets for {user_id}")
    return result.modified_count