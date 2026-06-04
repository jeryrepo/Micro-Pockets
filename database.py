from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
import os
from dotenv import load_dotenv

load_dotenv()

client = AsyncIOMotorClient(os.getenv("MONGODB_URI"))
db     = client["micropockets"]

users_col         = db["users"]
pockets_col       = db["pockets"]
transactions_col  = db["transactions"]
conversations_col = db["conversations"]   # NEW — stores every WhatsApp message


async def create_indexes():
    """
    Run once on startup. Safe to re-run — MongoDB ignores duplicates.
    """

    # ── users ──────────────────────────────────────────────────────────
    await users_col.create_index(
        [("whatsapp_number", ASCENDING)],
        unique=True,
        name="idx_users_whatsapp_number"
    )
    await users_col.create_index(
        [("setup_token", ASCENDING)],
        sparse=True,
        name="idx_users_setup_token"
    )

    # ── pockets ────────────────────────────────────────────────────────
    await pockets_col.create_index(
        [("user_id", ASCENDING), ("is_active", ASCENDING)],
        name="idx_pockets_user_active"
    )
    await pockets_col.create_index(
        [("user_id", ASCENDING), ("slug", ASCENDING)],
        unique=True,
        name="idx_pockets_user_slug"
    )

    # ── transactions ───────────────────────────────────────────────────
    await transactions_col.create_index(
        [("user_id", ASCENDING), ("timestamp", DESCENDING)],
        name="idx_transactions_user_time"
    )
    await transactions_col.create_index(
        [("pocket_id", ASCENDING), ("status", ASCENDING)],
        name="idx_transactions_pocket_status"
    )
    await transactions_col.create_index(
        [("status", ASCENDING)],
        name="idx_transactions_status"
    )

    # ── conversations ──────────────────────────────────────────────────
    await conversations_col.create_index(
        [("whatsapp_number", ASCENDING), ("timestamp", DESCENDING)],
        name="idx_conversations_user_time"
    )

    print("✅ MongoDB indexes created.")