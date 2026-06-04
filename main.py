import os
import json
import httpx
import secrets
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv
from database import users_col, pockets_col, transactions_col, conversations_col
from bson import ObjectId
from datetime import datetime, timezone

load_dotenv()

app = FastAPI(title="Micro-Pockets Core")


@app.on_event("startup")
async def startup_event():
    from database import create_indexes
    await create_indexes()
    print("✅ Micro-Pockets API is live.")


WA_VERIFY_TOKEN    = os.getenv("WA_VERIFY_TOKEN")
WA_ACCESS_TOKEN    = os.getenv("WA_ACCESS_TOKEN")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID")
ANDROID_REDIRECT   = os.getenv("ANDROID_REDIRECT")
IOS_SHORTCUT_URL   = os.getenv("IOS_SHORTCUT_URL")
BASE_URL           = os.getenv("BASE_URL")


# ─────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────

async def send_whatsapp_message(to: str, message: str):
    """Send a WhatsApp text message and log it to conversations."""
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type":  "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": message}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"WA SEND: {r.status_code}")

    # Log outbound message to conversations collection
    await log_message(
        whatsapp_number=to,
        direction="outbound",
        body=message
    )
    return r.json()


async def log_message(whatsapp_number: str, direction: str, body: str, extra: dict = None):
    """
    Save every message (inbound + outbound) to the conversations collection.
    direction: 'inbound' | 'outbound'
    This gives agents full conversation history context.
    """
    doc = {
        "whatsapp_number": whatsapp_number,
        "direction":       direction,
        "body":            body,
        "timestamp":       datetime.now(timezone.utc)
    }
    if extra:
        doc.update(extra)
    await conversations_col.insert_one(doc)


def serialize(doc) -> dict:
    """Convert MongoDB ObjectId/datetime fields to strings."""
    if doc is None:
        return None
    doc = dict(doc)
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            doc[k] = str(v)
        elif isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# ─────────────────────────────────────────────────────────────────────
#  1. WHATSAPP WEBHOOK — VERIFICATION
# ─────────────────────────────────────────────────────────────────────

@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == WA_VERIFY_TOKEN:
        print("Webhook verified.")
        return Response(content=params["hub.challenge"])
    return Response(status_code=403)


# ─────────────────────────────────────────────────────────────────────
#  2. WHATSAPP WEBHOOK — INBOUND MESSAGES
# ─────────────────────────────────────────────────────────────────────

@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request):
    data = await request.json()

    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Ignore status updates silently
        if "messages" not in value:
            return {"status": "ok"}

        msg      = value["messages"][0]
        sender   = msg["from"]
        msg_type = msg["type"]
        body     = msg["text"]["body"].strip() if msg_type == "text" else ""

        print(f"FROM: {sender} | BODY: {body}")

        # Log every inbound message to conversations
        await log_message(
            whatsapp_number=sender,
            direction="inbound",
            body=body,
            extra={"msg_type": msg_type, "wa_msg_id": msg.get("id")}
        )

        # Route based on user state
        user = await users_col.find_one({"whatsapp_number": sender})

        if not user:
            await handle_new_user(sender)
        elif not user.get("onboarding_complete"):
            await handle_onboarding_step(sender, body, user)
        else:
            await handle_existing_user(sender, body, user)

    except (KeyError, IndexError) as e:
        print(f"Parse error: {e}")

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
#  3. BANK NOTIFICATIONS (MacroDroid + iOS Shortcut)
# ─────────────────────────────────────────────────────────────────────

@app.post("/webhook/bank-notifications")
async def bank_inbound(request: Request):
    data = await request.json()
    print("BANK INBOUND:", json.dumps(data, indent=2))

    # Normalise key names — Android sends sms_body, iOS may send message_text
    sms_body   = data.get("sms_body") or data.get("message_text") or ""
    user_phone = data.get("user_phone") or data.get("phone") or "unknown"

    print(f"FROM: {user_phone} | SMS: {sms_body}")

    if not sms_body or user_phone == "unknown":
        return {"status": "ignored", "reason": "empty payload"}

    # Store raw SMS — Ingestion Agent will parse this in Sprint 3
    await transactions_col.insert_one({
        "user_phone":  user_phone,
        "raw_sms":     sms_body,
        "source":      "bank_sms",
        "status":      "pending_parse",
        "received_at": datetime.now(timezone.utc)
    })

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
#  4. DEVICE-AWARE CONNECT REDIRECT
# ─────────────────────────────────────────────────────────────────────

@app.get("/connect")
async def connect(request: Request, t: str = ""):
    ua = request.headers.get("user-agent", "").lower()
    print(f"CONNECT: token={t} ua={ua[:60]}")

    if "iphone" in ua or "ipad" in ua:
        return RedirectResponse(IOS_SHORTCUT_URL)
    elif "android" in ua:
        return RedirectResponse(ANDROID_REDIRECT)
    else:
        return HTMLResponse("""
        <html>
          <body style="font-family:sans-serif;text-align:center;
                       padding:60px 20px;background:#f5f5f5">
            <h2 style="color:#075E54">Micro-Pockets</h2>
            <p style="font-size:18px">Open this link on your phone.</p>
            <p style="color:#888">Works automatically on iPhone and Android.</p>
          </body>
        </html>
        """)


# ─────────────────────────────────────────────────────────────────────
#  5. SETUP COMPLETE CALLBACK
# ─────────────────────────────────────────────────────────────────────

@app.get("/setup/complete")
async def setup_complete(t: str = "", platform: str = ""):
    if not t:
        return {"status": "error", "reason": "missing token"}

    result = await users_col.find_one_and_update(
        {"setup_token": t},
        {"$set": {
            "bank_alerts_connected": True,
            "bank_alerts_platform":  platform,
            "setup_complete_at":     datetime.now(timezone.utc)
        }},
        return_document=True
    )

    if result:
        await send_whatsapp_message(
            result["whatsapp_number"],
            "✅ Bank alerts connected!\n\n"
            "I'll now automatically log your transactions.\n"
            "Reply *help* anytime to see what I can do."
        )
        return {"status": "ok"}

    return {"status": "error", "reason": "invalid token"}


# ─────────────────────────────────────────────────────────────────────
#  6. DEBUG ENDPOINTS
# ─────────────────────────────────────────────────────────────────────

@app.get("/test-db")
async def test_db():
    """Check all collection counts."""
    return {
        "status":        "connected",
        "users":         await users_col.count_documents({}),
        "pockets":       await pockets_col.count_documents({}),
        "transactions":  await transactions_col.count_documents({}),
        "conversations": await conversations_col.count_documents({})
    }


@app.get("/debug/user/{phone}")
async def debug_user(phone: str):
    """
    See everything stored for a user — profile, pockets, 
    transactions, and full conversation history.
    Usage: /debug/user/923034939390
    """
    user = await users_col.find_one({"whatsapp_number": phone})
    if not user:
        return {"error": "user not found"}

    user_id = user["_id"]

    pockets = await pockets_col.find(
        {"user_id": user_id}
    ).to_list(length=50)

    transactions = await transactions_col.find(
        {"user_id": user_id}
    ).sort("timestamp", -1).to_list(length=20)

    conversations = await conversations_col.find(
        {"whatsapp_number": phone}
    ).sort("timestamp", -1).to_list(length=50)

    def clean(docs):
        result = []
        for d in docs:
            d = dict(d)
            d["_id"] = str(d["_id"])
            for k, v in d.items():
                if isinstance(v, ObjectId):
                    d[k] = str(v)
                elif isinstance(v, datetime):
                    d[k] = v.isoformat()
            result.append(d)
        return result

    user = dict(user)
    user["_id"] = str(user["_id"])

    return {
        "user":          user,
        "pockets":       clean(pockets),
        "transactions":  clean(transactions),
        "conversations": clean(conversations)
    }


# ─────────────────────────────────────────────────────────────────────
#  7. ONBOARDING STATE MACHINE
# ─────────────────────────────────────────────────────────────────────

async def handle_new_user(sender: str):
    """First message from an unknown number — create user and start onboarding."""
    await users_col.insert_one({
        "whatsapp_number":       sender,
        "onboarding_complete":   False,
        "onboarding_step":       "awaiting_income",
        "created_at":            datetime.now(timezone.utc),
        "bank_alerts_connected": False
    })
    await send_whatsapp_message(
        sender,
        "👋 Welcome to *Micro-Pockets!*\n\n"
        "Your personal finance tracker, right here in WhatsApp.\n"
        "Quick setup — 3 questions.\n\n"
        "What's your *monthly income?*\n"
        "_e.g. 50000 or 1500_"
    )


async def handle_onboarding_step(sender: str, body: str, user: dict):
    """Process each onboarding reply and advance to next step."""
    step = user.get("onboarding_step", "awaiting_income")

    # ── Step 1: Income ────────────────────────────────────────────────
    if step == "awaiting_income":
        income_str = "".join(c for c in body if c.isdigit() or c == ".")
        if not income_str:
            await send_whatsapp_message(sender,
                "Please send your income as a number.\n_e.g. 50000_")
            return

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "financial_profile.monthly_income": float(income_str),
                "onboarding_step": "awaiting_currency"
            }}
        )
        await send_whatsapp_message(sender,
            f"Got it — *{income_str}* per month 💰\n\n"
            "*What currency do you use?*\n"
            "_e.g. PKR, USD, EUR_"
        )

    # ── Step 2: Currency ──────────────────────────────────────────────
    elif step == "awaiting_currency":
        currency = body.upper().strip()[:3]
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "base_currency":   currency,
                "onboarding_step": "awaiting_advisor_mode"
            }}
        )
        await send_whatsapp_message(sender,
            f"Got it — *{currency}* 👍\n\n"
            "*Should I alert you about spending?*\n\n"
            "1️⃣  Yes, alert me when I overspend\n"
            "2️⃣  Only when I ask\n"
            "3️⃣  Never\n\n"
            "_Reply 1, 2 or 3_"
        )

    # ── Step 3: Advisor mode ──────────────────────────────────────────
    elif step == "awaiting_advisor_mode":
        mode_map = {"1": "proactive", "2": "on_request", "3": "off"}
        mode  = mode_map.get(body.strip(), "on_request")
        token = secrets.token_urlsafe(16)

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "settings.advisor_mode":      mode,
                "settings.preferred_language": "en",
                "onboarding_step":            "awaiting_bank_setup",
                "onboarding_complete":        True,       # ← FIXED
                "setup_token":                token
            }}
        )

        # Create default pockets for this user
        user_doc = await users_col.find_one({"whatsapp_number": sender})
        await create_default_pockets(str(user_doc["_id"]))

        connect_url = f"{BASE_URL}/connect?t={token}"
        await send_whatsapp_message(sender,
            "Perfect! I've created your starter pockets:\n"
            "*Food · Transport · Bills · Shopping* 🎉\n\n"
            "Last step — tap below to connect your bank alerts.\n"
            "Works on iPhone and Android automatically.\n\n"
            f"👉 {connect_url}"
        )

    # ── Reminder if they message before tapping link ──────────────────
    elif step == "awaiting_bank_setup":
        token       = user.get("setup_token", "")
        connect_url = f"{BASE_URL}/connect?t={token}"
        await send_whatsapp_message(sender,
            "Please tap this link to connect your bank alerts 👇\n\n"
            f"👉 {connect_url}\n\n"
            "Once done you're all set!"
        )


# ─────────────────────────────────────────────────────────────────────
#  8. EXISTING USER HANDLER (placeholder until Sprint 3 agents)
# ─────────────────────────────────────────────────────────────────────

async def handle_existing_user(sender: str, body: str, user: dict):
    lower = body.lower()

    if lower in ["help", "hi", "hello", "hey"]:
        await send_whatsapp_message(sender,
            "Hey! 👋 Here's what you can do:\n\n"
            "💰 *add expense* — log a spend\n"
            "📊 *balance* — check your pockets\n"
            "📈 *how am I doing* — monthly summary\n"
            "➕ *create pocket [name] [amount]* — new budget\n"
            "❌ *delete last* — remove last transaction\n"
            "⚙️  *turn off advice* — silence the advisor\n\n"
            "_Interpreter Agent wiring up next sprint!_"
        )
    else:
        await send_whatsapp_message(sender,
            f"✅ Received: _{body}_\n\n"
            "_Agent pipeline coming in Sprint 3. "
            "Your message is logged._"
        )


# ─────────────────────────────────────────────────────────────────────
#  9. DEFAULT POCKET CREATION
# ─────────────────────────────────────────────────────────────────────

async def create_default_pockets(user_id: str):
    """Create 4 starter pockets when onboarding completes."""
    defaults = [
        ("Food",      200.0, 80),
        ("Transport", 100.0, 80),
        ("Bills",     300.0, 90),
        ("Shopping",  150.0, 80),
    ]
    for name, budget, threshold in defaults:
        slug = name.lower()
        # Skip if already exists (safe to re-run)
        exists = await pockets_col.find_one({
            "user_id": ObjectId(user_id),
            "slug":    slug
        })
        if exists:
            continue

        await pockets_col.insert_one({
            "user_id":             ObjectId(user_id),
            "name":                name,
            "slug":                slug,
            "type":                "permanent",
            "allocated_budget":    budget,
            "current_balance":     budget,
            "alert_threshold_pct": threshold,
            "alert_snoozed":       False,
            "snooze_reset_date":   None,
            "is_active":           True,
            "expires_at":          None,
            "created_at":          datetime.now(timezone.utc)
        })
    print(f"✅ Default pockets created for user {user_id}")