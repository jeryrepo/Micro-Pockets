"""
main.py
───────
FastAPI gateway — thin routing layer only.
All business logic lives in agent files.
"""

import os
import json
import httpx
import secrets
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv
from core.database import users_col, pockets_col, transactions_col, conversations_col
from bson import ObjectId
from datetime import datetime, timezone

load_dotenv()

app = FastAPI(title="Micro-Pockets Core")


@app.on_event("startup")
async def startup_event():
    from core.database import create_indexes
    await create_indexes()
    print("✅ Micro-Pockets API is live.")


WA_VERIFY_TOKEN    = os.getenv("WA_VERIFY_TOKEN")
WA_ACCESS_TOKEN    = os.getenv("WA_ACCESS_TOKEN")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID")
ANDROID_REDIRECT   = os.getenv("ANDROID_REDIRECT")
IOS_SHORTCUT_URL   = os.getenv("IOS_SHORTCUT_URL")

# Starter pocket names shown to user during onboarding
STARTER_POCKETS = ["Food", "Transport", "Bills", "Shopping"]


# ─────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────

async def send_whatsapp_message(to: str, message: str):
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
    await _log_message(to, "outbound", message)
    return r.json()


async def _log_message(
    whatsapp_number: str,
    direction: str,
    body: str,
    extra: dict = None
):
    doc = {
        "whatsapp_number": whatsapp_number,
        "direction":       direction,
        "body":            body,
        "timestamp":       datetime.now(timezone.utc)
    }
    if extra:
        doc.update(extra)
    await conversations_col.insert_one(doc)


def get_base_url(request: Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        scheme = request.headers.get("x-forwarded-proto", "https")
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url).rstrip("/")


def _clean(doc) -> dict:
    if not doc:
        return {}
    doc = dict(doc)
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            doc[k] = str(v)
        elif isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# ─────────────────────────────────────────────────────────────────────
#  1. WHATSAPP — VERIFICATION
# ─────────────────────────────────────────────────────────────────────

@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == WA_VERIFY_TOKEN:
        print("Webhook verified.")
        return Response(content=params["hub.challenge"])
    return Response(status_code=403)


# ─────────────────────────────────────────────────────────────────────
#  2. WHATSAPP — INBOUND MESSAGES
# ─────────────────────────────────────────────────────────────────────

@app.post("/webhook/whatsapp")
async def whatsapp_inbound(request: Request):
    data     = await request.json()
    base_url = get_base_url(request)

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return {"status": "ok"}

        msg      = value["messages"][0]
        sender   = msg["from"]
        msg_type = msg["type"]
        body     = msg["text"]["body"].strip() if msg_type == "text" else ""
        language      = "en"
        language_name = "English"

        # ── Handle voice notes ────────────────────────────────────────
        if msg_type == "audio":
            media_id = msg["audio"]["id"]
            print(f"VOICE NOTE from {sender} — media_id: {media_id}")
            await send_whatsapp_message(sender, "_🎤 Listening..._")

            from agents.voice_agent import transcribe
            result = await transcribe(media_id)

            if not result["success"] or not result["transcript"]:
                await send_whatsapp_message(sender,
                    "Sorry, I couldn't understand the voice note. "
                    "Please try again or type your message.")
                return {"status": "ok"}

            body          = result["transcript"]
            language      = result["language"]
            language_name = result["language_name"]
            print(f"VOICE TRANSCRIPT ({language_name}): {body}")

        # ── Ignore non-text non-audio messages ────────────────────────
        elif msg_type not in ("text",):
            await send_whatsapp_message(sender,
                "I can only read text and voice notes for now. "
                "Please type or send a voice message.")
            return {"status": "ok"}

        print(f"FROM: {sender} | LANG: {language} | BODY: {body}")

        await _log_message(sender, "inbound", body,
            extra={
                "msg_type":     msg_type,
                "wa_msg_id":    msg.get("id"),
                "language":     language,
                "language_name": language_name
            })

        user = await users_col.find_one({"whatsapp_number": sender})

        if not user:
            await _onboard_new(sender)
        elif not user.get("onboarding_complete"):
            await _onboard_step(sender, body, user, base_url)
        else:
            await _route(sender, body, user, base_url, language, language_name)

    except (KeyError, IndexError) as e:
        print(f"Parse error: {e}")

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
#  3. BANK NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────

@app.post("/webhook/bank-notifications")
async def bank_inbound(request: Request):
    data = await request.json()
    print("BANK INBOUND:", json.dumps(data, indent=2))

    sms_body   = data.get("sms_body") or data.get("message_text") or ""
    user_phone = data.get("user_phone") or data.get("phone") or "unknown"

    if not sms_body or user_phone == "unknown":
        return {"status": "ignored"}

    from agents.interpreter_agent import interpret
    intent = await interpret(sms_body)
    print(f"BANK INTENT: {intent['intent']} | {intent['amount']} | {intent['merchant']}")

    if intent["intent"] == "bank_sms":
        user = await users_col.find_one({"whatsapp_number": user_phone})
        if user:
            from agents.ingestion_agent import process
            await process(user_phone, intent, user, send_whatsapp_message)
        else:
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
    print(f"CONNECT — token={t}")

    if "iphone" in ua or "ipad" in ua:
        return RedirectResponse(IOS_SHORTCUT_URL)
    elif "android" in ua:
        return RedirectResponse(ANDROID_REDIRECT)
    else:
        return HTMLResponse("""
        <html><body style="font-family:sans-serif;text-align:center;
                           padding:60px 20px;background:#f5f5f5">
          <h2 style="color:#075E54">Micro-Pockets</h2>
          <p style="font-size:18px">Open this link on your phone.</p>
          <p style="color:#888">Works automatically on iPhone and Android.</p>
        </body></html>
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
        await send_whatsapp_message(result["whatsapp_number"],
            "✅ Bank alerts connected!\n\n"
            "I'll now automatically log your transactions.\n"
            "Reply *help* anytime to see what I can do."
        )
        return {"status": "ok"}

    return {"status": "error", "reason": "invalid token"}


# ─────────────────────────────────────────────────────────────────────
#  6. DEBUG / HEALTH
# ─────────────────────────────────────────────────────────────────────

@app.get("/test-db")
async def test_db():
    return {
        "status":        "connected",
        "users":         await users_col.count_documents({}),
        "pockets":       await pockets_col.count_documents({}),
        "transactions":  await transactions_col.count_documents({}),
        "conversations": await conversations_col.count_documents({})
    }


@app.get("/debug/user/{phone}")
async def debug_user(phone: str):
    user = await users_col.find_one({"whatsapp_number": phone})
    if not user:
        return {"error": "user not found"}

    user_id = user["_id"]
    pockets = await pockets_col.find({"user_id": user_id}).to_list(50)
    txns    = await transactions_col.find(
        {"user_id": user_id}).sort("timestamp", -1).to_list(20)
    convos  = await conversations_col.find(
        {"whatsapp_number": phone}).sort("timestamp", -1).to_list(30)

    return {
        "user":          _clean(user),
        "pockets":       [_clean(p) for p in pockets],
        "transactions":  [_clean(t) for t in txns],
        "conversations": [_clean(c) for c in convos]
    }


@app.get("/debug/base-url")
async def debug_base_url(request: Request):
    return {"detected_base_url": get_base_url(request)}


# ─────────────────────────────────────────────────────────────────────
#  7. SMART ROUTER
# ─────────────────────────────────────────────────────────────────────

async def _route(
    sender: str,
    body: str,
    user: dict,
    base_url: str,
    language: str = "en",
    language_name: str = "English"
):
    from agents.interpreter_agent import interpret

    lower = body.lower().strip()

    # ── Special keywords always handled first ─────────────────────────
    if lower == "link":
        await _send_connect_link(sender, user, base_url)
        return

    if lower in ("android", "macrodroid"):
        await _send_macrodroid_guide(sender, user, base_url)
        return

    if lower in ("iphone", "ios", "apple", "shortcut", "iphone setup"):
        await _send_ios_guide(sender, user, base_url)
        return

    # ── Check if user has a pending multi-step flow ───────────────────
    # Re-fetch user to get latest state (pending_intent may have just been set)
    user = await users_col.find_one({"whatsapp_number": sender})
    pending = user.get("pending_intent", {}) if user else {}
    pending_action = pending.get("action") if pending else None

    # ── Cancel any pending flow first ─────────────────────────────────
    cancel_words = {
        "cancel", "discard", "stop", "no", "never mind",
        "nevermind", "abort", "quit", "exit", "cancel it",
        "discard it", "forget it", "skip it", "nope"
    }
    if pending_action and lower in cancel_words:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$unset": {"pending_intent": "", "pending_txn_id": ""}}
        )
        await send_whatsapp_message(sender, "Cancelled. What else can I help with?")
        return

    # Pending pocket creation — waiting for name
    if pending_action == "awaiting_pocket_name":
        # User is providing the pocket name
        pocket_name = body.strip()
        slug        = pocket_name.lower().replace(" ", "-")
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":      "awaiting_pocket_budget",
                "pocket_name": pocket_name,
                "pocket_slug": slug
            }}}
        )
        currency = user.get("base_currency", "PKR")
        await send_whatsapp_message(sender,
            f"Got it — *{pocket_name}* pocket.\n\n"
            f"How much is the monthly budget for this pocket?\n"
            f"_e.g. 5000 {currency}_"
        )
        return

    if pending_action == "awaiting_pocket_budget":
        amount = _parse_amount(body)
        if not amount or amount <= 0:
            await send_whatsapp_message(sender,
                "Please send a valid budget amount.\n"
                "_e.g. 5000 or 5k_\n\n"
                "Or reply *cancel* to discard.")
            return

        pocket_name = pending.get("pocket_name", "New Pocket")
        slug        = pending.get("pocket_slug", pocket_name.lower().replace(" ", "-"))
        currency    = user.get("base_currency", "PKR")
        user_id     = user["_id"]

        # Create the pocket
        existing = await pockets_col.find_one({
            "user_id": user_id, "slug": slug, "is_active": True
        })
        if existing:
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$unset": {"pending_intent": ""}}
            )
            await send_whatsapp_message(sender,
                f"You already have a *{pocket_name}* pocket.\n"
                f"To update its budget: _change {slug} budget to {amount:.0f}_")
            return

        await pockets_col.insert_one({
            "user_id":             user_id,
            "name":                pocket_name.title(),
            "slug":                slug,
            "type":                "permanent",
            "allocated_budget":    amount,
            "current_balance":     amount,
            "alert_threshold_pct": 80,
            "alert_snoozed":       False,
            "snooze_reset_date":   None,
            "is_active":           True,
            "expires_at":          None,
            "created_at":          datetime.now(timezone.utc)
        })

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$unset": {"pending_intent": ""}}
        )

        await send_whatsapp_message(sender,
            f"✅ *{pocket_name.title()}* pocket created!\n"
            f"Budget: *{amount:.0f} {currency}*\n\n"
            f"Start logging: _spent 500 on {slug}_"
        )
        return

    # ── Normal flow — run through interpreter then route ─────────────
    intent = await interpret(body)
    i      = intent["intent"]

    # Use language from voice transcription if provided,
    # otherwise use what interpreter detected
    detected_lang      = language if language != "en" else intent.get("language", "en")
    detected_lang_name = language_name if language != "en" else intent.get("language_name", "English")

    print(f"ROUTER: {sender} → {i} | lang: {detected_lang}")

    # Conversational query → conversation agent
    if i == "conversational_query" or (
        i == "unknown" and len(body.split()) > 4
    ):
        from agents.conversation_agent import handle as convo_handle
        await convo_handle(
            sender, body, user,
            detected_lang, detected_lang_name,
            send_whatsapp_message
        )
        return

    # READ intents → query_agent
    if i in (
        "query_balance",
        "query_transactions",
        "monthly_summary",
        "query_income",
        "request_advice",
        "stock_query",
        "help",
        "unknown"
    ):
        from agents.query_agent import handle as query_handle
        await query_handle(sender, intent, user, send_whatsapp_message)

    # WRITE intents → interaction_agent
    else:
        from agents.interaction_agent import handle as interaction_handle
        await interaction_handle(sender, intent, user, send_whatsapp_message)


# ─────────────────────────────────────────────────────────────────────
#  8. ONBOARDING STATE MACHINE
# ─────────────────────────────────────────────────────────────────────

ONBOARDING_STEPS = [
    "awaiting_name",
    "awaiting_income",
    "awaiting_currency",
    "awaiting_advisor_mode",
    "awaiting_pocket_choice",
    "awaiting_food_budget",
    "awaiting_transport_budget",
    "awaiting_bills_budget",
    "awaiting_shopping_budget",
    "awaiting_bank_setup_choice",
    "awaiting_bank_setup",
]


def _parse_amount(text: str) -> float:
    """
    Parse amount from user input handling:
      - Plain numbers: 50000, 150000
      - Shorthand k/m: 150k, 1.5k, 2m
      - Inline currency stripped: 150k pkr, PKR 50000, $1000
    Returns float or 0 if unparseable.
    """
    import re
    text = text.lower().strip()

    # Remove currency symbols and words
    for symbol in ["pkr", "usd", "eur", "gbp", "$", "€", "£",
                   "rupees", "rupee", "dollars", "dollar", "euros"]:
        text = text.replace(symbol, " ")

    text = text.strip()

    # Match number with optional k/m suffix
    match = re.search(r"([\d,]+\.?\d*)\s*([km])?", text)
    if not match:
        return 0.0

    num_str    = match.group(1).replace(",", "")
    multiplier = match.group(2)

    try:
        amount = float(num_str)
        if multiplier == "k":
            amount *= 1_000
        elif multiplier == "m":
            amount *= 1_000_000
        return amount
    except ValueError:
        return 0.0


async def _onboard_new(sender: str):
    """First message — create user skeleton and ask for name."""
    await users_col.insert_one({
        "whatsapp_number":       sender,
        "onboarding_complete":   False,
        "onboarding_step":       "awaiting_name",
        "created_at":            datetime.now(timezone.utc),
        "bank_alerts_connected": False
    })
    await send_whatsapp_message(sender,
        "👋 Welcome to *Micro-Pockets!*\n\n"
        "Your personal finance tracker, right here in WhatsApp. "
        "No app needed.\n\n"
        "Let's get you set up in 4 quick steps.\n\n"
        "*What's your name?*"
    )


async def _onboard_step(sender: str, body: str, user: dict, base_url: str):
    step  = user.get("onboarding_step", "awaiting_income")
    lower = body.lower().strip()

    # ── Step 0: Name ─────────────────────────────────────────────────
    if step == "awaiting_name":
        name = body.strip().title()
        if len(name) < 2 or len(name) > 30:
            await send_whatsapp_message(sender,
                "Please send your first name.\n_e.g. Hassan_")
            return

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "name":            name,
                "onboarding_step": "awaiting_income"
            }}
        )
        await send_whatsapp_message(sender,
            f"Nice to meet you, *{name}!* 👋\n\n"
            "What's your *monthly income?*\n"
            "_e.g. 50000 or 150000_"
        )

    # ── Step 1: Income ────────────────────────────────────────────────
    elif step == "awaiting_income":
        income = _parse_amount(body)
        if not income or income <= 0:
            await send_whatsapp_message(sender,
                "Please send your income as a number.\n"
                "_e.g. 50000 or 150k_")
            return

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "financial_profile.monthly_income": income,
                "onboarding_step": "awaiting_currency"
            }}
        )
        user_doc = await users_col.find_one({"whatsapp_number": sender})
        name     = user_doc.get("name", "")
        await send_whatsapp_message(sender,
            f"Got it{', ' + name if name else ''} — "
            f"*{income:.0f}* per month 💰\n\n"
            "*What currency do you use?*\n"
            "_e.g. PKR, USD, EUR_"
        )

    # ── Step 2: Currency ──────────────────────────────────────────────
    elif step == "awaiting_currency":
        # Extract only alphabetic characters, take first 3
        import re
        alpha_only = re.sub(r"[^a-zA-Z]", " ", body).strip()
        words      = [w for w in alpha_only.upper().split() if len(w) >= 2]
        currency   = words[0][:3] if words else ""

        if not currency or len(currency) < 2:
            await send_whatsapp_message(sender,
                "Please send your currency code.\n"
                "_e.g. PKR, USD, EUR_")
            return
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
        mode = mode_map.get(body.strip(), "on_request")

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {
                "settings.advisor_mode":       mode,
                "settings.preferred_language": "en",
                "onboarding_step":             "awaiting_pocket_choice"
            }}
        )

        pocket_names = " · ".join([f"*{p}*" for p in STARTER_POCKETS])
        await send_whatsapp_message(sender,
            f"Got it! 👍\n\n"
            f"Should I create these starter pockets for you?\n\n"
            f"{pocket_names}\n\n"
            f"These are the most common budget categories. "
            f"You can rename, delete, or add more anytime.\n\n"
            f"Reply *yes* to set them up now or *later* to skip."
        )

    # ── Step 4: Pocket choice ─────────────────────────────────────────
    elif step == "awaiting_pocket_choice":
        if lower in ("yes", "y", "sure", "ok", "yep", "yeah"):
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$set": {"onboarding_step": "awaiting_food_budget"}}
            )
            user_doc = await users_col.find_one({"whatsapp_number": sender})
            currency = user_doc.get("base_currency", "PKR")
            await send_whatsapp_message(sender,
                "Let's set your budgets one by one.\n\n"
                f"💰 *Food pocket*\n"
                f"How much do you spend on food per month?\n"
                f"_e.g. 5000 {currency}_"
            )
        else:
            # Skip pocket creation — do it later
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
            )
            await send_whatsapp_message(sender,
                "No problem! You can create pockets anytime.\n"
                "Just type: _create pocket food 5000_\n\n"
                "Moving on to the next step..."
            )
            # Immediately show bank setup choice
            await _ask_bank_setup(sender, user, base_url)

    # ── Step 4a-d: Pocket budgets ─────────────────────────────────────
    elif step in (
        "awaiting_food_budget",
        "awaiting_transport_budget",
        "awaiting_bills_budget",
        "awaiting_shopping_budget"
    ):
        await _handle_pocket_budget_step(sender, body, user, step, base_url)

    # ── Step 5: Bank setup choice ─────────────────────────────────────
    elif step == "awaiting_bank_setup_choice":
        await _handle_bank_setup_choice(sender, body, user, base_url)

    # ── Step 6: Awaiting bank setup (reminder if they message first) ──
    elif step == "awaiting_bank_setup":
        await _send_connect_link(sender, user, base_url)


async def _handle_pocket_budget_step(
    sender: str,
    body: str,
    user: dict,
    step: str,
    base_url: str
):
    """Handle budget collection for each starter pocket."""
    user_doc = await users_col.find_one({"whatsapp_number": sender})
    currency = user_doc.get("base_currency", "PKR")
    user_id  = user_doc["_id"]

    amount = _parse_amount(body)
    if not amount or amount <= 0:
        pocket_name = step.replace("awaiting_", "").replace("_budget", "").title()
        await send_whatsapp_message(sender,
            f"Please send a number for your *{pocket_name}* budget.\n"
            f"_e.g. 5000 or 5k_")
        return

    # Map step to pocket info and next step
    step_map = {
        "awaiting_food_budget":      ("Food",      "food",      "awaiting_transport_budget"),
        "awaiting_transport_budget": ("Transport", "transport", "awaiting_bills_budget"),
        "awaiting_bills_budget":     ("Bills",     "bills",     "awaiting_shopping_budget"),
        "awaiting_shopping_budget":  ("Shopping",  "shopping",  None),
    }

    name, slug, next_step = step_map[step]

    # Create the pocket
    await pockets_col.insert_one({
        "user_id":             user_id,
        "name":                name,
        "slug":                slug,
        "type":                "permanent",
        "allocated_budget":    amount,
        "current_balance":     amount,
        "alert_threshold_pct": 80,
        "alert_snoozed":       False,
        "snooze_reset_date":   None,
        "is_active":           True,
        "expires_at":          None,
        "created_at":          datetime.now(timezone.utc)
    })

    print(f"✅ Created {name} pocket: {amount} {currency} for {sender}")

    if next_step:
        # Ask for next pocket budget
        next_name = next_step.replace("awaiting_", "").replace("_budget", "").title()
        pocket_emoji = {
            "Transport": "🚗",
            "Bills":     "💡",
            "Shopping":  "🛍️"
        }
        emoji = pocket_emoji.get(next_name, "💰")

        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"onboarding_step": next_step}}
        )
        await send_whatsapp_message(sender,
            f"✅ *{name}* → *{amount:.0f} {currency}*\n\n"
            f"{emoji} *{next_name} pocket*\n"
            f"How much per month?\n"
            f"_e.g. 3000 {currency}_"
        )
    else:
        # All 4 pockets done — show summary and move to bank setup
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
        )

        # Build pocket summary
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(10)

        user_doc2 = await users_col.find_one({"whatsapp_number": sender})
        name      = user_doc2.get("name", "")
        currency  = user_doc2.get("base_currency", "PKR")

        name_possessive = f"{name}'s pockets are ready!" if name else "Your pockets are ready!"
        summary_lines = [f"✅ *{name_possessive}*\n"]
        for p in pockets:
            summary_lines.append(
                f"• *{p['name']}*: {p['allocated_budget']:.0f} {currency}/month"
            )

        await send_whatsapp_message(sender, "\n".join(summary_lines))
        await _ask_bank_setup(sender, user, base_url)


async def _ask_bank_setup(sender: str, user: dict, base_url: str):
    """Ask user if they want to set up bank alerts now."""
    await send_whatsapp_message(sender,
        "🏦 *One more thing — Bank Alerts*\n\n"
        "I can *automatically log* transactions from your bank SMS. "
        "No manual entry needed.\n\n"
        "Want to set it up now?\n\n"
        "📱 *ANDROID* — uses MacroDroid (free app, 2 min setup)\n"
        "🍎 *IPHONE* — uses iOS Shortcuts (built-in, 1 tap)\n"
        "⏭️ *LATER* — skip for now, type *link* anytime to get it\n\n"
        "_Reply ANDROID, IPHONE or LATER_"
    )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
    )


async def _handle_bank_setup_choice(
    sender: str,
    body: str,
    user: dict,
    base_url: str
):
    """Handle the user's bank setup platform choice."""
    lower = body.lower().strip()

    if any(w in lower for w in ["android", "macrodroid"]):
        await _send_macrodroid_guide(sender, user, base_url)

    elif any(w in lower for w in ["iphone", "ios", "apple", "shortcut"]):
        await _send_ios_guide(sender, user, base_url)

    elif any(w in lower for w in ["later", "skip", "no", "not now", "next"]):
        await _complete_onboarding(sender, user, skip_bank=True)

    else:
        await send_whatsapp_message(sender,
            "Please reply with:\n"
            "• *ANDROID* — for MacroDroid setup\n"
            "• *IPHONE* — for iOS Shortcuts setup\n"
            "• *LATER* — skip for now"
        )


async def _send_macrodroid_guide(sender: str, user: dict, base_url: str):
    """Send complete MacroDroid setup guide in one message."""
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"

    await send_whatsapp_message(sender,
        "📱 *MacroDroid Setup — Android*\n"
        "_(Takes about 2 minutes)_\n\n"
        "*Step 1* — Install MacroDroid\n"
        "Search 'MacroDroid' on Play Store and install it.\n\n"
        "*Step 2* — Create a new Macro\n"
        "Open MacroDroid → tap ➕ → name it 'Bank Alerts'\n\n"
        "*Step 3* — Set the Trigger\n"
        "Tap *Triggers* → *SMS / MMS Received*\n"
        "Set sender filter to your bank's name\n"
        "_(e.g. 'HBL', 'MCB', 'UBL', 'BOP')_\n\n"
        "*Step 4* — Add HTTP Action\n"
        "Tap *Actions* → *Networking* → *HTTP Request*\n"
        f"URL: `{base_url}/webhook/bank-notifications`\n"
        "Method: POST\n"
        "Body (JSON):\n"
        "```\n"
        "{\n"
        f'  "user_phone": "+{sender}",\n'
        '  "sms_body": "[SMS Body]"\n'
        "}\n"
        "```\n\n"
        "*Step 5* — Enable the Macro\n"
        "Toggle it ON and tap Save.\n\n"
        "✅ Done! Send yourself a test bank SMS to verify.\n\n"
        f"👉 Or tap here to go straight to MacroDroid:\n{connect_url}"
    )

    await _complete_onboarding(sender, user, skip_bank=False)


async def _send_ios_guide(sender: str, user: dict, base_url: str):
    """Send complete iOS Shortcut setup guide in one message."""
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"

    await send_whatsapp_message(sender,
        "🍎 *iOS Shortcuts Setup — iPhone*\n"
        "_(Takes about 1 minute)_\n\n"
        "*Step 1* — Open Shortcuts app\n"
        "It's pre-installed on every iPhone.\n\n"
        "*Step 2* — New Automation\n"
        "Tap *Automation* tab → ➕ → *Message Received*\n"
        "Set sender to your bank contact name\n"
        "_(Save your bank's number as a contact first, e.g. 'HBL Bank')_\n\n"
        "*Step 3* — Add Actions\n"
        "• *Get Details of Messages* → select *Message Content*\n"
        "• *Get Contents of URL*\n"
        f"  URL: `{base_url}/webhook/bank-notifications`\n"
        "  Method: POST\n"
        "  Body (JSON):\n"
        "```\n"
        "{\n"
        f'  "user_phone": "+{sender}",\n'
        '  "sms_body": [Message Content]\n'
        "}\n"
        "```\n\n"
        "*Step 4* — Critical setting ⚠️\n"
        "Turn OFF *'Ask Before Running'*\n"
        "Otherwise it won't fire automatically.\n\n"
        "*Step 5* — Tap Done\n\n"
        "✅ Done! Send yourself a test bank SMS to verify.\n\n"
        f"👉 Or tap here to install the pre-built Shortcut:\n{connect_url}"
    )

    await _complete_onboarding(sender, user, skip_bank=False)


async def _ensure_token(user: dict) -> str:
    """Get existing setup token or generate a new one."""
    token = user.get("setup_token")
    if not token:
        token = secrets.token_urlsafe(16)
        await users_col.update_one(
            {"whatsapp_number": user["whatsapp_number"]},
            {"$set": {"setup_token": token}}
        )
    return token


async def _complete_onboarding(sender: str, user: dict, skip_bank: bool):
    """Mark onboarding complete and send welcome message."""
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {
            "onboarding_complete": True,
            "onboarding_step":     "done"
        }}
    )

    # Check if user has any pockets
    user_doc = await users_col.find_one({"whatsapp_number": sender})
    name     = user_doc.get("name", "")
    pockets  = await pockets_col.find(
        {"user_id": user_doc["_id"], "is_active": True}
    ).to_list(10)

    pocket_hint = ""
    if not pockets:
        pocket_hint = "\n\n💡 _Create a pocket: create pocket food 5000_"

    bank_hint = ""
    if skip_bank:
        bank_hint = "\n🏦 _Set up bank alerts anytime — just type *link*_"

    greeting = f"{name}, you're all set!" if name else "You're all set!"
    await send_whatsapp_message(sender,
        f"🎉 *{greeting}*\n\n"
        "Here's what you can do:\n\n"
        "💰 _spent 500 on food_ — log expense\n"
        "📊 _balance_ — check pockets\n"
        "📈 _how am I doing_ — monthly summary\n"
        "❌ _delete last_ — undo last entry\n"
        "➕ _create pocket travel 5000_ — new pocket\n"
        "⚙️  _turn off advice_ — advisor settings\n\n"
        "Type *help* anytime for the full guide."
        f"{pocket_hint}"
        f"{bank_hint}"
    )


async def _send_connect_link(sender: str, user: dict, base_url: str):
    """Send the bank setup link — triggered by typing 'link'."""
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"

    await send_whatsapp_message(sender,
        "🏦 *Bank Alerts Setup*\n\n"
        "Tap the link below on your phone:\n"
        f"👉 {connect_url}\n\n"
        "• iPhone → installs iOS Shortcut automatically\n"
        "• Android → opens MacroDroid on Play Store\n\n"
        "Need step-by-step instructions?\n"
        "Reply *ANDROID* or *IPHONE* for the full guide."
    )


# ─────────────────────────────────────────────────────────────────────
#  9. DEFAULT POCKET CREATION (used only if needed)
# ─────────────────────────────────────────────────────────────────────

async def _create_default_pockets(user_id: str):
    """Fallback — only used if pockets needed without user input."""
    defaults = [
        ("Food",      5000.0, 80),
        ("Transport", 3000.0, 80),
        ("Bills",     8000.0, 90),
        ("Shopping",  3000.0, 80),
    ]
    for name, budget, threshold in defaults:
        slug = name.lower()
        if await pockets_col.find_one({"user_id": ObjectId(user_id), "slug": slug}):
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
    print(f"✅ Default pockets created for {user_id}")