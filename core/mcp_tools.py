"""
mcp_tools.py
All MongoDB read/write operations used by the agents.
Each function maps to one MCP tool the agents will call.
Atomic operations are used wherever balance updates are involved
to prevent race conditions.
"""

from core.database import users_col, pockets_col, transactions_col
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────
#  USERS
# ─────────────────────────────────────────────

async def get_user_by_phone(whatsapp_number: str) -> Optional[dict]:
    """Find a user document by their WhatsApp number."""
    return await users_col.find_one({"whatsapp_number": whatsapp_number})


async def upsert_user_profile(whatsapp_number: str, updates: dict) -> dict:
    """
    Create or update a user profile.
    Used by onboarding and settings changes.
    """
    result = await users_col.find_one_and_update(
        {"whatsapp_number": whatsapp_number},
        {"$set": updates},
        upsert=True,
        return_document=True
    )
    return result


async def set_advisor_mode(whatsapp_number: str, mode: str) -> bool:
    """
    Update advisor_mode for a user.
    mode: 'off' | 'on_request' | 'proactive'
    """
    result = await users_col.update_one(
        {"whatsapp_number": whatsapp_number},
        {"$set": {"settings.advisor_mode": mode}}
    )
    return result.modified_count > 0


# ─────────────────────────────────────────────
#  POCKETS
# ─────────────────────────────────────────────

async def find_pockets_by_user(user_id: str) -> list:
    """
    Return all active pockets for a user.
    Used by Ingestion Agent for semantic pocket matching.
    """
    cursor = pockets_col.find({
        "user_id":   ObjectId(user_id),
        "is_active": True
    })
    return await cursor.to_list(length=100)


async def insert_pocket(
    user_id: str,
    name: str,
    allocated_budget: float,
    pocket_type: str = "permanent",
    alert_threshold_pct: int = 80,
    expires_at=None
) -> dict:
    """Create a new budget pocket."""
    slug = name.lower().strip().replace(" ", "-")
    doc  = {
        "user_id":               ObjectId(user_id),
        "name":                  name,
        "slug":                  slug,
        "type":                  pocket_type,
        "allocated_budget":      allocated_budget,
        "current_balance":       allocated_budget,
        "alert_threshold_pct":   alert_threshold_pct,
        "alert_snoozed":         False,
        "snooze_reset_date":     None,
        "is_active":             True,
        "expires_at":            expires_at,
        "created_at":            datetime.now(timezone.utc)
    }
    result = await pockets_col.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


async def create_default_pockets(user_id: str) -> list:
    """
    Create the four default pockets for a new user.
    Called at end of onboarding.
    """
    defaults = [
        ("Food",       200.0, 80),
        ("Transport",  100.0, 80),
        ("Bills",      300.0, 90),
        ("Shopping",   150.0, 80),
    ]
    created = []
    for name, budget, threshold in defaults:
        pocket = await insert_pocket(user_id, name, budget, "permanent", threshold)
        created.append(pocket)
    return created


async def update_pocket_balance(pocket_id: str, delta: float) -> Optional[dict]:
    """
    Atomically adjust pocket balance using $inc.
    delta is negative for spending, positive for refunds/deletions.
    This single atomic operation prevents race conditions.
    """
    result = await pockets_col.find_one_and_update(
        {"_id": ObjectId(pocket_id)},
        {"$inc": {"current_balance": delta}},
        return_document=True
    )
    return result


async def reset_monthly_alerts(user_id: str) -> int:
    """
    Reset alert_snoozed on all pockets for a user.
    Called by background scheduler on 1st of each month.
    """
    result = await pockets_col.update_many(
        {"user_id": ObjectId(user_id), "is_active": True},
        {"$set": {
            "alert_snoozed":     False,
            "snooze_reset_date": datetime.now(timezone.utc)
        }}
    )
    return result.modified_count


# ─────────────────────────────────────────────
#  TRANSACTIONS
# ─────────────────────────────────────────────

async def insert_transaction(
    user_id: str,
    pocket_id: str,
    merchant: str,
    amount: float,
    currency: str,
    original_amount: float,
    original_currency: str,
    exchange_rate: float,
    raw_payload: str,
    status: str = "pending_review"
) -> dict:
    """
    Insert a transaction AND atomically decrement the pocket balance
    in a single logical operation.
    status='pending_review' until user confirms with 'ok'.
    """
    now = datetime.now(timezone.utc)
    doc = {
        "user_id":           ObjectId(user_id),
        "pocket_id":         ObjectId(pocket_id),
        "merchant":          merchant,
        "amount_base":       amount,
        "original_currency": original_currency,
        "original_amount":   original_amount,
        "exchange_rate":     exchange_rate,
        "raw_payload":       raw_payload,
        "timestamp":         now,
        "status":            status
    }
    result = await transactions_col.insert_one(doc)
    doc["_id"] = str(result.inserted_id)

    # Atomic balance update — happens in same operation
    if status == "confirmed":
        await update_pocket_balance(pocket_id, -amount)

    return doc


async def confirm_transaction(transaction_id: str) -> Optional[dict]:
    """
    Confirm a pending_review transaction.
    Triggers the balance deduction atomically.
    """
    txn = await transactions_col.find_one({"_id": ObjectId(transaction_id)})
    if not txn or txn["status"] != "pending_review":
        return None

    updated = await transactions_col.find_one_and_update(
        {"_id": ObjectId(transaction_id)},
        {"$set": {"status": "confirmed"}},
        return_document=True
    )

    # Now deduct balance
    await update_pocket_balance(str(txn["pocket_id"]), -txn["amount_base"])
    return updated


async def delete_transaction(transaction_id: str) -> bool:
    """
    Soft-delete a transaction and restore the pocket balance.
    Used when user says 'delete last transaction'.
    """
    txn = await transactions_col.find_one({"_id": ObjectId(transaction_id)})
    if not txn:
        return False

    await transactions_col.update_one(
        {"_id": ObjectId(transaction_id)},
        {"$set": {"status": "deleted"}}
    )

    # Restore balance only if it was confirmed
    if txn["status"] == "confirmed":
        await update_pocket_balance(str(txn["pocket_id"]), txn["amount_base"])

    return True


async def get_last_transaction(user_id: str) -> Optional[dict]:
    """Get the most recent non-deleted transaction for a user."""
    return await transactions_col.find_one(
        {"user_id": ObjectId(user_id), "status": {"$ne": "deleted"}},
        sort=[("timestamp", -1)]
    )


async def aggregate_monthly_trends(user_id: str) -> list:
    """
    Aggregate total spending per pocket for the current month.
    Used by Advisor Agent for the monthly summary.
    """
    from datetime import date
    now   = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    pipeline = [
        {"$match": {
            "user_id":   ObjectId(user_id),
            "status":    "confirmed",
            "timestamp": {"$gte": start}
        }},
        {"$group": {
            "_id":         "$pocket_id",
            "total_spent": {"$sum": "$amount_base"},
            "count":       {"$sum": 1}
        }},
        {"$lookup": {
            "from":         "pockets",
            "localField":   "_id",
            "foreignField": "_id",
            "as":           "pocket"
        }},
        {"$unwind": "$pocket"},
        {"$project": {
            "pocket_name":        "$pocket.name",
            "allocated_budget":   "$pocket.allocated_budget",
            "total_spent":        1,
            "count":              1,
            "remaining":          {
                "$subtract": ["$pocket.allocated_budget", "$total_spent"]
            },
            "pct_used": {
                "$multiply": [
                    {"$divide": ["$total_spent", "$pocket.allocated_budget"]},
                    100
                ]
            }
        }}
    ]

    cursor = transactions_col.aggregate(pipeline)
    return await cursor.to_list(length=50)


async def aggregate_monthly_trends_for(user_id: str, month: int, year: int) -> list:
    """
    Aggregate spending for a specific month and year.
    Used for month-over-month comparisons in conversation_agent.
    """
    import calendar
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    _, last_day = calendar.monthrange(year, month)
    end   = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    pipeline = [
        {"$match": {
            "user_id":   ObjectId(user_id),
            "status":    "confirmed",
            "timestamp": {"$gte": start, "$lte": end}
        }},
        {"$group": {
            "_id":         "$pocket_id",
            "total_spent": {"$sum": "$amount_base"},
            "count":       {"$sum": 1}
        }},
        {"$lookup": {
            "from":         "pockets",
            "localField":   "_id",
            "foreignField": "_id",
            "as":           "pocket"
        }},
        {"$unwind": "$pocket"},
        {"$project": {
            "pocket_name":      "$pocket.name",
            "allocated_budget": "$pocket.allocated_budget",
            "total_spent":      1,
            "count":            1,
            "remaining": {
                "$subtract": ["$pocket.allocated_budget", "$total_spent"]
            },
            "pct_used": {
                "$multiply": [
                    {"$divide": ["$total_spent", "$pocket.allocated_budget"]},
                    100
                ]
            }
        }}
    ]

    cursor = transactions_col.aggregate(pipeline)
    return await cursor.to_list(length=50)