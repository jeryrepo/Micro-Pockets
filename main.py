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
import re
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
STARTER_POCKETS    = ["Food", "Transport", "Bills", "Shopping"]


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


def _parse_amount(text: str) -> float:
    """Parse amount supporting plain numbers, 1k/2.5k/1m, and inline currency."""
    text = text.lower().strip()
    for symbol in ["pkr", "usd", "eur", "gbp", "$", "€", "£",
                   "rupees", "rupee", "dollars", "dollar", "euros"]:
        text = text.replace(symbol, " ")
    text = text.strip()
    match = re.search(r"([\d,]+\.?\d*)\s*([km])?", text)
    if not match:
        return 0.0
    try:
        amount = float(match.group(1).replace(",", ""))
        if match.group(2) == "k":
            amount *= 1_000
        elif match.group(2) == "m":
            amount *= 1_000_000
        return amount
    except ValueError:
        return 0.0


def _normalize_phone(phone: str) -> str:
    """Strip leading + so numbers match how WhatsApp stores them."""
    return phone.lstrip("+")


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
        body          = msg["text"]["body"].strip() if msg_type == "text" else ""
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

        elif msg_type not in ("text",):
            await send_whatsapp_message(sender,
                "I can only read text and voice notes for now.")
            return {"status": "ok"}

        print(f"FROM: {sender} | LANG: {language} | BODY: {body}")

        await _log_message(sender, "inbound", body, extra={
            "msg_type":      msg_type,
            "wa_msg_id":     msg.get("id"),
            "language":      language,
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

    lookup_phone = _normalize_phone(user_phone)

    from agents.interpreter_agent import interpret
    intent = await interpret(sms_body)
    print(f"BANK INTENT: {intent['intent']} | {intent['amount']} | {intent['merchant']}")

    if intent["intent"] == "bank_sms":
        user = await users_col.find_one({"whatsapp_number": lookup_phone})
        if user:
            from agents.ingestion_agent import process
            await process(lookup_phone, intent, user, send_whatsapp_message)
        else:
            print(f"BANK: user not found for {lookup_phone}")
            await transactions_col.insert_one({
                "user_phone":  lookup_phone,
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

    # ── Special keywords always first ─────────────────────────────────
    if lower == "link":
        await _send_connect_link(sender, user, base_url)
        return
    if lower in ("android", "macrodroid"):
        await _send_macrodroid_guide(sender, user, base_url)
        return
    if lower in ("iphone", "ios", "apple", "shortcut", "iphone setup"):
        await _send_ios_guide(sender, user, base_url)
        return

    # ── Re-fetch user for latest pending state ─────────────────────────
    user = await users_col.find_one({"whatsapp_number": sender})
    pending        = user.get("pending_intent", {}) if user else {}
    pending_action = pending.get("action") if pending else None

    # ── Cancel any pending flow ────────────────────────────────────────
    cancel_words = {
        "cancel", "discard", "stop", "no", "never mind", "nevermind",
        "abort", "quit", "exit", "cancel it", "discard it",
        "forget it", "skip it", "nope", "ignore"
    }
    if pending_action and lower in cancel_words:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$unset": {"pending_intent": "", "pending_txn_id": ""}}
        )
        await send_whatsapp_message(sender, "Cancelled. What else can I help with?")
        return

    # ── Pending: awaiting deduct or add decision ───────────────────────
    if pending_action == "awaiting_deduct_or_add":
        await _handle_deduct_or_add(sender, lower, pending, user)
        return

    # ── Pending: awaiting pocket name for deduction ────────────────────
    if pending_action == "awaiting_deduct_pocket":
        await _handle_deduct_pocket_reply(sender, body, pending, user)
        return

    # ── Pending: awaiting pocket name for addition ─────────────────────
    if pending_action == "awaiting_add_pocket":
        await _handle_add_pocket_reply(sender, body, pending, user)
        return

    # ── Pending: awaiting pocket name ─────────────────────────────────
    if pending_action == "awaiting_pocket_name":
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
            f"How much is the monthly budget?\n"
            f"_e.g. 5000 {currency}_"
        )
        return

    # ── Pending: awaiting pocket budget ───────────────────────────────
    if pending_action == "awaiting_pocket_budget":
        amount = _parse_amount(body)
        if not amount or amount <= 0:
            await send_whatsapp_message(sender,
                "Please send a valid budget amount.\n"
                "_e.g. 5000 or 5k_\n\nOr reply *cancel* to discard.")
            return

        pocket_name = pending.get("pocket_name", "New Pocket")
        slug        = pending.get("pocket_slug", pocket_name.lower().replace(" ", "-"))
        currency    = user.get("base_currency", "PKR")
        user_id     = user["_id"]

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
                f"To update: _change {slug} budget to {amount:.0f}_")
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

    # ── Pending: remember merchant ─────────────────────────────────────
    if pending_action == "remember_merchant":
        if lower in ("yes", "y", "sure", "ok", "yep", "yeah"):
            from agents.ingestion_agent import save_merchant_mapping
            await save_merchant_mapping(
                user["_id"],
                pending.get("merchant", ""),
                pending.get("slug", "")
            )
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$unset": {"pending_intent": ""}}
            )
            await send_whatsapp_message(sender,
                f"✅ Got it! I'll always add "
                f"*{pending.get('merchant')}* to "
                f"*{pending.get('name')}* from now on."
            )
        else:
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$unset": {"pending_intent": ""}}
            )
            await send_whatsapp_message(sender, "👍 No problem.")
        return

    # ── Pending: user replying with pocket name for expense ────────────
    if pending_action == "add_expense":
        await _handle_add_expense_pocket_reply(sender, body, pending, user)
        return

    # ── Normal flow ────────────────────────────────────────────────────
    intent = await interpret(body)
    i      = intent["intent"]

    detected_lang      = language if language != "en" else intent.get("language", "en")
    detected_lang_name = language_name if language != "en" else intent.get("language_name", "English")

    print(f"ROUTER: {sender} → {i} | lang: {detected_lang}")

    if i in (
        "query_balance", "query_transactions", "monthly_summary",
        "query_income", "request_advice", "stock_query",
        "conversational_query", "help", "unknown"
    ):
        from agents.query_agent import handle as query_handle
        await query_handle(sender, intent, user, send_whatsapp_message)
    else:
        from agents.interaction_agent import handle as interaction_handle
        await interaction_handle(sender, intent, user, send_whatsapp_message)


# ─────────────────────────────────────────────────────────────────────
#  BANK TRANSACTION FLOW HANDLERS
# ─────────────────────────────────────────────────────────────────────

async def _handle_deduct_or_add(
    sender: str, lower: str, pending: dict, user: dict
):
    """Handle user reply to 'deduct or add?' question."""
    amount      = pending.get("amount", 0)
    currency    = pending.get("currency", user.get("base_currency", "PKR"))
    merchant    = pending.get("merchant", "")
    pocket_slug = pending.get("pocket_slug")
    pocket_name = pending.get("pocket_name", "")

    if lower in ("deduct", "deduction", "subtract", "expense", "spent", "out"):
        if pocket_slug:
            # Pocket already known — create pending transaction
            pocket = await pockets_col.find_one({
                "user_id": user["_id"], "slug": pocket_slug, "is_active": True
            })
            if pocket:
                result = await transactions_col.insert_one({
                    "user_id":           user["_id"],
                    "pocket_id":         pocket["_id"],
                    "merchant":          merchant or "Bank transaction",
                    "amount_base":       amount,
                    "original_currency": currency,
                    "original_amount":   amount,
                    "exchange_rate":     1.0,
                    "raw_payload":       pending.get("raw", ""),
                    "timestamp":         datetime.now(timezone.utc),
                    "status":            "pending_review",
                    "source":            "bank_sms"
                })
                update = {"pending_txn_id": str(result.inserted_id)}
                if pending.get("gemini_guess"):
                    update["pending_merchant_learn"] = {
                        "merchant": merchant,
                        "slug":     pocket["slug"],
                        "name":     pocket["name"]
                    }
                await users_col.update_one(
                    {"whatsapp_number": sender},
                    {"$set": update, "$unset": {"pending_intent": ""}}
                )
                new_balance = pocket["current_balance"] - amount
                await send_whatsapp_message(sender,
                    f"*{amount:.0f} {currency}* — {merchant} → *{pocket['name']}*\n"
                    f"Balance after: *{new_balance:.2f} {currency}*\n\n"
                    "Reply *ok* to confirm or *cancel* to discard."
                )
                return

        # No pocket known — ask which one
        pockets = await pockets_col.find(
            {"user_id": user["_id"], "is_active": True}
        ).to_list(20)
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":   "awaiting_deduct_pocket",
                "amount":   amount,
                "currency": currency,
                "merchant": merchant,
                "raw":      pending.get("raw", "")
            }}}
        )
        await send_whatsapp_message(sender,
            f"Which pocket should *{amount:.0f} {currency}* be deducted from?\n\n"
            f"{pocket_list}\n\n_Reply with the pocket name_"
        )

    elif lower in ("add", "credit", "income", "received", "in"):
        if pocket_slug:
            # Pocket already known — add directly
            updated = await pockets_col.find_one_and_update(
                {"user_id": user["_id"], "slug": pocket_slug, "is_active": True},
                {"$inc": {"current_balance": amount}},
                return_document=True
            )
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$unset": {"pending_intent": ""}}
            )
            await send_whatsapp_message(sender,
                f"✅ *{amount:.0f} {currency}* from *{merchant}* "
                f"added to *{updated['name']}*\n"
                f"New balance: *{updated['current_balance']:.2f} {currency}*"
            )
            return

        # No pocket known — ask which one
        pockets = await pockets_col.find(
            {"user_id": user["_id"], "is_active": True}
        ).to_list(20)
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"pending_intent": {
                "action":   "awaiting_add_pocket",
                "amount":   amount,
                "currency": currency,
                "merchant": merchant
            }}}
        )
        await send_whatsapp_message(sender,
            f"Which pocket should *{amount:.0f} {currency}* be added to?\n\n"
            f"{pocket_list}\n\n_Reply with the pocket name_"
        )

    elif lower in ("discard", "skip", "ignore", "no"):
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$unset": {"pending_intent": ""}}
        )
        await send_whatsapp_message(sender, "👍 Transaction ignored.")

    else:
        await send_whatsapp_message(sender,
            "Please reply:\n"
            "• *deduct* — it was an expense\n"
            "• *add* — money was received\n"
            "• *discard* — ignore this transaction"
        )


async def _handle_deduct_pocket_reply(
    sender: str, body: str, pending: dict, user: dict
):
    """User replied with pocket name for deduction."""
    slug     = body.lower().strip().replace(" ", "-")
    amount   = pending.get("amount", 0)
    currency = pending.get("currency", user.get("base_currency", "PKR"))
    merchant = pending.get("merchant", "")
    user_id  = user["_id"]

    pocket = await pockets_col.find_one({
        "user_id": user_id, "slug": slug, "is_active": True
    })
    if not pocket:
        pocket = await pockets_col.find_one({
            "user_id":   user_id,
            "name":      {"$regex": f"^{re.escape(body.strip())}$", "$options": "i"},
            "is_active": True
        })
    if not pocket:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await send_whatsapp_message(sender,
            f"I couldn't find *{body.strip()}*. Choose from:\n{pocket_list}")
        return

    result = await transactions_col.insert_one({
        "user_id":           user_id,
        "pocket_id":         pocket["_id"],
        "merchant":          merchant or "Bank transaction",
        "amount_base":       amount,
        "original_currency": currency,
        "original_amount":   amount,
        "exchange_rate":     1.0,
        "raw_payload":       pending.get("raw", ""),
        "timestamp":         datetime.now(timezone.utc),
        "status":            "pending_review",
        "source":            "bank_sms"
    })
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set":   {"pending_txn_id": str(result.inserted_id)},
         "$unset": {"pending_intent": ""}}
    )
    new_balance = pocket["current_balance"] - amount
    await send_whatsapp_message(sender,
        f"*{amount:.0f} {currency}* — {merchant} → *{pocket['name']}*\n"
        f"Balance after: *{new_balance:.2f} {currency}*\n\n"
        "Reply *ok* to confirm or *cancel* to discard."
    )


async def _handle_add_pocket_reply(
    sender: str, body: str, pending: dict, user: dict
):
    """User replied with pocket name for adding money."""
    slug     = body.lower().strip().replace(" ", "-")
    amount   = pending.get("amount", 0)
    currency = pending.get("currency", user.get("base_currency", "PKR"))
    merchant = pending.get("merchant", "")
    user_id  = user["_id"]

    pocket = await pockets_col.find_one({
        "user_id": user_id, "slug": slug, "is_active": True
    })
    if not pocket:
        pocket = await pockets_col.find_one({
            "user_id":   user_id,
            "name":      {"$regex": f"^{re.escape(body.strip())}$", "$options": "i"},
            "is_active": True
        })
    if not pocket:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await send_whatsapp_message(sender,
            f"I couldn't find *{body.strip()}*. Choose from:\n{pocket_list}")
        return

    updated = await pockets_col.find_one_and_update(
        {"_id": pocket["_id"]},
        {"$inc": {"current_balance": amount}},
        return_document=True
    )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$unset": {"pending_intent": ""}}
    )
    await send_whatsapp_message(sender,
        f"✅ *{amount:.0f} {currency}* from *{merchant}* "
        f"added to *{updated['name']}*\n"
        f"New balance: *{updated['current_balance']:.2f} {currency}*"
    )


async def _handle_add_expense_pocket_reply(
    sender: str, body: str, pending: dict, user: dict
):
    """User replied with pocket name for a manually-asked expense."""
    slug     = body.lower().strip().replace(" ", "-")
    amount   = pending.get("amount", 0)
    currency = pending.get("currency", user.get("base_currency", "PKR"))
    merchant = pending.get("merchant", "")
    source   = pending.get("source", "whatsapp")
    user_id  = user["_id"]

    pocket = await pockets_col.find_one({
        "user_id": user_id, "slug": slug, "is_active": True
    })
    if not pocket:
        pocket = await pockets_col.find_one({
            "user_id":   user_id,
            "name":      {"$regex": f"^{re.escape(body.strip())}$", "$options": "i"},
            "is_active": True
        })
    if not pocket:
        pockets = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(20)
        pocket_list = "\n".join([f"• *{p['name']}*" for p in pockets])
        await send_whatsapp_message(sender,
            f"I couldn't find *{body.strip()}*. Choose from:\n{pocket_list}\n\n"
            "_Reply with the pocket name_"
        )
        return

    result = await transactions_col.insert_one({
        "user_id":           user_id,
        "pocket_id":         pocket["_id"],
        "merchant":          merchant or "Bank transaction",
        "amount_base":       amount,
        "original_currency": currency,
        "original_amount":   amount,
        "exchange_rate":     1.0,
        "raw_payload":       pending.get("raw", ""),
        "timestamp":         datetime.now(timezone.utc),
        "status":            "pending_review",
        "source":            source
    })
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set":   {"pending_txn_id": str(result.inserted_id)},
         "$unset": {"pending_intent": ""}}
    )
    new_balance = pocket["current_balance"] - amount
    await send_whatsapp_message(sender,
        f"*{amount:.0f} {currency}* — {merchant or 'transaction'} → *{pocket['name']}*\n"
        f"Balance after: *{new_balance:.2f} {currency}*\n\n"
        "Reply *ok* to confirm or *cancel* to discard."
    )


# ─────────────────────────────────────────────────────────────────────
#  8. ONBOARDING STATE MACHINE
# ─────────────────────────────────────────────────────────────────────

async def _classify_onboarding(body: str, step: str) -> str:
    """
    Use Gemini to classify an ambiguous onboarding response.
    Has keyword fallback so obvious inputs never fail.
    """
    lower = body.lower().strip()

    # ── Fast keyword fallback — no Gemini needed for obvious inputs ────
    if step == "awaiting_pocket_choice":
        yes_words = {"yes", "y", "sure", "ok", "yep", "yeah", "create",
                     "do it", "go ahead", "setup", "set up", "set them up",
                     "create them", "yes create", "sure do it", "definitely"}
        no_words  = {"no", "nope", "later", "skip", "not now", "next",
                     "ill do it", "maybe later", "no thanks"}
        if any(w in lower for w in yes_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'yes'")
            return "yes"
        if any(w in lower for w in no_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'later'")
            return "later"

    elif step == "awaiting_bank_setup_choice":
        android_words = {"android", "macrodroid", "samsung", "play store",
                         "andriod", "androd", "androud", "androind", "google"}
        iphone_words  = {"iphone", "ios", "apple", "shortcut", "shortcuts",
                         "ipad", "i phone", "i os"}
        later_words   = {"later", "skip", "no", "not now", "next", "maybe"}
        if any(w in lower for w in android_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'android'")
            return "android"
        if any(w in lower for w in iphone_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'iphone'")
            return "iphone"
        if any(w in lower for w in later_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'later'")
            return "later"

    elif step == "awaiting_setup_confirmation":
        done_words = {"done", "yes", "y", "ok", "sure", "yep", "yeah",
                      "finished", "complete", "completed", "ready", "all set",
                      "set up", "setup done", "its done", "it's done"}
        later_words = {"later", "skip", "no", "not now", "not done"}
        if any(w in lower for w in done_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'done'")
            return "done"
        if any(w in lower for w in later_words):
            print(f"CLASSIFIER KEYWORD [{step}]: '{body}' → 'later'")
            return "later"

    # ── Gemini fallback for truly ambiguous inputs ─────────────────────
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
        location=os.getenv("GCP_LOCATION", "us-central1")
    )
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

    if step == "awaiting_pocket_choice":
        prompt = (
            f"The user was asked: 'Should I create starter budget pockets (Food, Transport, Bills, Shopping) for you?'\n"
            f"User replied: '{body}'\n\n"
            f"Classify their intent:\n"
            f"- yes: if they want to create pockets now — any form of agreement like 'yes', 'yes create', 'sure', 'go ahead', 'do it', 'create them', 'yeah', 'ok', 'create', 'set them up'\n"
            f"- later: if they want to skip or do it later — 'no', 'later', 'skip', 'not now', 'ill do it myself'\n\n"
            f"Reply with ONLY one word: yes OR later"
        )
        valid = ("yes", "later")
        default = "later"

    elif step == "awaiting_bank_setup_choice":
        prompt = (
            f"The user was asked to choose how to set up bank alerts: ANDROID, IPHONE, or LATER.\n"
            f"User replied: '{body}'\n\n"
            f"Classify their intent:\n"
            f"- android: if they mention android, macrodroid, samsung, google, play store, or have a typo like 'andriod', 'androd', 'androud'\n"
            f"- iphone: if they mention iphone, ios, apple, shortcuts, ipad\n"
            f"- later: if they want to skip, do it later, or are unsure\n\n"
            f"Reply with ONLY one word: android OR iphone OR later"
        )
        valid = ("android", "iphone", "later")
        default = "later"

    elif step == "awaiting_setup_confirmation":
        prompt = (
            f"The user just finished setting up bank alerts on their phone.\n"
            f"They were asked: 'Reply done when you've finished setting up.'\n"
            f"User replied: '{body}'\n\n"
            f"Have they finished the setup or do they want to do it later?\n"
            f"Reply with ONLY one word: done OR later"
        )
        valid = ("done", "later")
        default = "done"

    else:
        return body.lower().strip()

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=10
            )
        )
        raw     = (response.text or "").strip().lower()
        # Extract just the first word in case Gemini adds punctuation
        result  = raw.split()[0].rstrip(".,!?") if raw.split() else ""
        print(f"ONBOARDING CLASSIFIER [{step}]: '{body}' → raw='{raw}' result='{result}'")
        return result if result in valid else default
    except Exception as e:
        print(f"ONBOARDING CLASSIFIER error: {e}")
        return default


async def _onboarding_help(sender: str, body: str, step: str, user: dict):
    """
    Called when user sends 'help' or a confused message during onboarding.
    Gemini knows which step the user is on and gives contextual guidance.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
        location=os.getenv("GCP_LOCATION", "us-central1")
    )
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

    name = user.get("name", "")

    step_context = {
        "awaiting_name":              "We asked for the user's first name.",
        "awaiting_income":            "We asked for their monthly income as a number e.g. 50000 or 150k.",
        "awaiting_currency":          "We asked for their currency code e.g. PKR, USD, EUR.",
        "awaiting_advisor_mode":      "We asked them to reply 1 (proactive alerts), 2 (on request), or 3 (never).",
        "awaiting_pocket_choice":     "We asked if they want to create starter pockets (Food, Transport, Bills, Shopping). Reply yes or later.",
        "awaiting_food_budget":       "We asked how much they spend on food per month. Send a number e.g. 5000.",
        "awaiting_transport_budget":  "We asked how much they spend on transport per month. Send a number e.g. 3000.",
        "awaiting_bills_budget":      "We asked how much they spend on bills per month. Send a number e.g. 8000.",
        "awaiting_shopping_budget":   "We asked how much they spend on shopping per month. Send a number e.g. 4000.",
        "awaiting_bank_setup_choice": "We asked if they want to set up bank alerts. Reply ANDROID, IPHONE, or LATER.",
        "awaiting_setup_confirmation": "We sent them a MacroDroid/iOS Shortcuts setup guide and asked them to reply 'done' when finished.",
    }

    context = step_context.get(step, "We are setting up their Micro-Pockets account.")

    try:
        response = client.models.generate_content(
            model=model,
            contents=(
                f"You are Micro-Pockets, a WhatsApp finance assistant helping a user during onboarding setup.\n"
                f"User's name: {name or 'unknown'}\n"
                f"Current onboarding step: {step}\n"
                f"Context: {context}\n"
                f"User message: '{body}'\n\n"
                f"The user seems confused or needs help. Give them a short, friendly explanation "
                f"of what they need to do at this step. Be concise — max 3 sentences. "
                f"WhatsApp format. Don't mention step names or technical terms."
            ),
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=200
            )
        )
        reply = (response.text or "").strip()
        print(f"ONBOARDING HELP [{step}]: {reply[:80]}")
        await send_whatsapp_message(sender, reply)
    except Exception as e:
        print(f"ONBOARDING HELP error: {e}")
        await send_whatsapp_message(sender,
            "I'm here to help! Type *help* and I'll guide you through this step.")


async def _onboard_new(sender: str):
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

    # ── Help request at any onboarding step ───────────────────────────
    help_triggers = {"help", "?", "what", "how", "i dont understand",
                     "i don't understand", "confused", "stuck", "what do i do",
                     "what should i type", "i need help", "huh"}
    if lower in help_triggers or lower.startswith("help"):
        await _onboarding_help(sender, body, step, user)
        return

    if step == "awaiting_name":
        name = body.strip().title()
        if len(name) < 2 or len(name) > 30:
            await send_whatsapp_message(sender,
                "Please send your first name.\n_e.g. Hassan_")
            return
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"name": name, "onboarding_step": "awaiting_income"}}
        )
        await send_whatsapp_message(sender,
            f"Nice to meet you, *{name}!* 👋\n\n"
            "What's your *monthly income?*\n_e.g. 50000 or 150k_"
        )

    elif step == "awaiting_income":
        income = _parse_amount(body)
        if not income or income <= 0:
            await send_whatsapp_message(sender,
                "Please send your income as a number.\n_e.g. 50000 or 150k_")
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
            "*What currency do you use?*\n_e.g. PKR, USD, EUR_"
        )

    elif step == "awaiting_currency":
        alpha_only = re.sub(r"[^a-zA-Z]", " ", body).strip()
        words      = [w for w in alpha_only.upper().split() if len(w) >= 2]
        currency   = words[0][:3] if words else ""
        if not currency or len(currency) < 2:
            await send_whatsapp_message(sender,
                "Please send your currency code.\n_e.g. PKR, USD, EUR_")
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
            "3️⃣  Never\n\n_Reply 1, 2 or 3_"
        )

    elif step == "awaiting_advisor_mode":
        mode_map = {"1": "proactive", "2": "on_request", "3": "off"}
        mode     = mode_map.get(body.strip(), "on_request")
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
            "You can rename, delete, or add more anytime.\n\n"
            "Reply *yes* to set them up now or *later* to skip."
        )

    elif step == "awaiting_pocket_choice":
        decision = await _classify_onboarding(body, "awaiting_pocket_choice")
        if decision == "yes":
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
            await users_col.update_one(
                {"whatsapp_number": sender},
                {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
            )
            await send_whatsapp_message(sender,
                "No problem! Create pockets anytime: _create pocket food 5000_\n\n"
                "Moving on...")
            await _ask_bank_setup(sender, user, base_url)

    elif step in (
        "awaiting_food_budget", "awaiting_transport_budget",
        "awaiting_bills_budget", "awaiting_shopping_budget"
    ):
        await _handle_pocket_budget_step(sender, body, user, step, base_url)

    elif step == "awaiting_bank_setup_choice":
        await _handle_bank_setup_choice(sender, body, user, base_url)

    elif step == "awaiting_setup_confirmation":
        decision = await _classify_onboarding(body, "awaiting_setup_confirmation")
        if decision == "done":
            await _complete_onboarding(sender, user, skip_bank=False)
        else:
            await _complete_onboarding(sender, user, skip_bank=True)

    elif step == "awaiting_bank_setup":
        await _send_connect_link(sender, user, base_url)


async def _handle_pocket_budget_step(
    sender: str, body: str, user: dict, step: str, base_url: str
):
    user_doc = await users_col.find_one({"whatsapp_number": sender})
    currency = user_doc.get("base_currency", "PKR")
    user_id  = user_doc["_id"]

    amount = _parse_amount(body)
    if not amount or amount <= 0:
        pocket_name = step.replace("awaiting_", "").replace("_budget", "").title()
        await send_whatsapp_message(sender,
            f"Please send a number for *{pocket_name}* budget.\n"
            f"_e.g. 5000 or 5k_")
        return

    step_map = {
        "awaiting_food_budget":      ("Food",      "food",      "awaiting_transport_budget"),
        "awaiting_transport_budget": ("Transport", "transport", "awaiting_bills_budget"),
        "awaiting_bills_budget":     ("Bills",     "bills",     "awaiting_shopping_budget"),
        "awaiting_shopping_budget":  ("Shopping",  "shopping",  None),
    }
    name, slug, next_step = step_map[step]

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
    print(f"✅ Created {name} pocket: {amount} {currency}")

    if next_step:
        next_name = next_step.replace("awaiting_", "").replace("_budget", "").title()
        emoji = {"Transport": "🚗", "Bills": "💡", "Shopping": "🛍️"}.get(next_name, "💰")
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"onboarding_step": next_step}}
        )
        await send_whatsapp_message(sender,
            f"✅ *{name}* → *{amount:.0f} {currency}*\n\n"
            f"{emoji} *{next_name} pocket*\n"
            f"How much per month?\n_e.g. 3000 {currency}_"
        )
    else:
        await users_col.update_one(
            {"whatsapp_number": sender},
            {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
        )
        pockets         = await pockets_col.find(
            {"user_id": user_id, "is_active": True}
        ).to_list(10)
        user_doc2       = await users_col.find_one({"whatsapp_number": sender})
        name_str        = user_doc2.get("name", "")
        curr            = user_doc2.get("base_currency", "PKR")
        name_possessive = f"{name_str}'s pockets are ready!" if name_str else "Your pockets are ready!"
        lines           = [f"✅ *{name_possessive}*\n"]
        for p in pockets:
            lines.append(f"• *{p['name']}*: {p['allocated_budget']:.0f} {curr}/month")
        await send_whatsapp_message(sender, "\n".join(lines))
        await _ask_bank_setup(sender, user, base_url)


async def _ask_bank_setup(sender: str, user: dict, base_url: str):
    await send_whatsapp_message(sender,
        "🏦 *One more thing — Bank Alerts*\n\n"
        "I can *automatically log* transactions from your bank SMS.\n\n"
        "📱 *ANDROID* — MacroDroid (free, 2 min)\n"
        "🍎 *IPHONE* — iOS Shortcuts (built-in, 1 tap)\n"
        "⏭️ *LATER* — type *link* anytime\n\n"
        "_Reply ANDROID, IPHONE or LATER_"
    )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"onboarding_step": "awaiting_bank_setup_choice"}}
    )


async def _handle_bank_setup_choice(
    sender: str, body: str, user: dict, base_url: str
):
    decision = await _classify_onboarding(body, "awaiting_bank_setup_choice")
    if decision == "android":
        await _send_macrodroid_guide(sender, user, base_url)
    elif decision == "iphone":
        await _send_ios_guide(sender, user, base_url)
    else:
        await _complete_onboarding(sender, user, skip_bank=True)


async def _send_macrodroid_guide(sender: str, user: dict, base_url: str):
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"
    await send_whatsapp_message(sender,
        "📱 *MacroDroid Setup — Android*\n_(Takes about 2 minutes)_\n\n"
        "*Step 1* — Install MacroDroid\nSearch 'MacroDroid' on Play Store.\n\n"
        "*Step 2* — Create a new Macro\nOpen MacroDroid → tap ➕ → name it 'Bank Alerts'\n\n"
        "*Step 3* — Set the Trigger\nTap *Triggers* → *SMS / MMS Received*\n"
        "Set sender filter to your bank's name _(e.g. 'HBL', 'MCB', 'UBL', 'BOP')_\n\n"
        "*Step 4* — Add HTTP Action\nTap *Actions* → *Networking* → *HTTP Request*\n"
        f"URL: `{base_url}/webhook/bank-notifications`\n"
        "Method: POST\nBody (JSON):\n```\n"
        "{\n"
        f'  "user_phone": "+{sender}",\n'
        '  "sms_body": "[SMS Body]"\n'
        "}\n```\n\n"
        "*Step 5* — Enable the Macro\nToggle it ON and tap Save.\n\n"
        f"✅ Done!\n\n👉 Or tap here:\n{connect_url}"
    )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"onboarding_step": "awaiting_setup_confirmation"}}
    )
    await send_whatsapp_message(sender,
        "Reply *done* when you've finished setting up 👆\n\n"
        "Or reply *later* to set it up another time."
    )


async def _send_ios_guide(sender: str, user: dict, base_url: str):
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"
    await send_whatsapp_message(sender,
        "🍎 *iOS Shortcuts Setup — iPhone*\n_(Takes about 1 minute)_\n\n"
        "*Step 1* — Open Shortcuts app\nPre-installed on every iPhone.\n\n"
        "*Step 2* — New Automation\nTap *Automation* → ➕ → *Message Received*\n"
        "Set sender to your bank contact name\n\n"
        "*Step 3* — Add Actions\n"
        "• *Get Details of Messages* → Message Content\n"
        "• *Get Contents of URL*\n"
        f"  URL: `{base_url}/webhook/bank-notifications`\n"
        "  Method: POST\n  Body (JSON):\n```\n"
        "{\n"
        f'  "user_phone": "+{sender}",\n'
        '  "sms_body": [Message Content]\n'
        "}\n```\n\n"
        "*Step 4* — ⚠️ Turn OFF *'Ask Before Running'*\n\n"
        "*Step 5* — Tap Done\n\n"
        f"✅ Done!\n\n👉 Or tap here:\n{connect_url}"
    )
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"onboarding_step": "awaiting_setup_confirmation"}}
    )
    await send_whatsapp_message(sender,
        "Reply *done* when you've finished setting up 👆\n\n"
        "Or reply *later* to set it up another time."
    )


async def _ensure_token(user: dict) -> str:
    token = user.get("setup_token")
    if not token:
        token = secrets.token_urlsafe(16)
        await users_col.update_one(
            {"whatsapp_number": user["whatsapp_number"]},
            {"$set": {"setup_token": token}}
        )
    return token


async def _complete_onboarding(sender: str, user: dict, skip_bank: bool):
    await users_col.update_one(
        {"whatsapp_number": sender},
        {"$set": {"onboarding_complete": True, "onboarding_step": "done"}}
    )
    user_doc = await users_col.find_one({"whatsapp_number": sender})
    name     = user_doc.get("name", "")
    pockets  = await pockets_col.find(
        {"user_id": user_doc["_id"], "is_active": True}
    ).to_list(10)

    pocket_hint = "\n\n💡 _Create a pocket: create pocket food 5000_" if not pockets else ""
    bank_hint   = "\n🏦 _Set up bank alerts anytime — type *link*_" if skip_bank else ""
    greeting    = f"{name}, you're all set!" if name else "You're all set!"

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
        f"{pocket_hint}{bank_hint}"
    )


async def _send_connect_link(sender: str, user: dict, base_url: str):
    token       = await _ensure_token(user)
    connect_url = f"{base_url}/connect?t={token}"
    await send_whatsapp_message(sender,
        "🏦 *Bank Alerts Setup*\n\n"
        f"👉 {connect_url}\n\n"
        "• iPhone → iOS Shortcut installs automatically\n"
        "• Android → opens MacroDroid on Play Store\n\n"
        "Need a guide? Reply *ANDROID* or *IPHONE*."
    )


# ─────────────────────────────────────────────────────────────────────
#  9. DEFAULT POCKET CREATION (fallback)
# ─────────────────────────────────────────────────────────────────────

async def _create_default_pockets(user_id: str):
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