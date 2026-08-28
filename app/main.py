"""VICOBA AI Digital Treasurer — FastAPI application.

Two-phase architecture (from the review's Section 7):
  1. POST /parse  → rule-based (or LLM) parser returns structured intent
  2. POST /commit → deterministic Python executes the validated intent atomically

Authentication: 3-tier committee RBAC (Mwenyekiti → Mhazinaji → Katibu) with
PIN logins. Every mutation is logged with the actor identity.
"""
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Form, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, ledger, members, wa_bridge
from .commits import (
    commit_contribute,
    commit_exit,
    commit_expense,
    commit_fee,
    commit_fine,
    commit_loan,
    commit_payout,
    commit_register,
    commit_repay,
)
from .errors import AppError, DuplicateCommit
from .parser import ParsedIntent, parse as rule_parse
from .reports import (
    group_position,
    gawio_estimate,
    meeting_sheet,
    member_statement,
    who_hasnt_paid_today,
)

logger = logging.getLogger("vicoba")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="VICOBA Digital Treasurer", lifespan=lifespan)
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")


# ── Helpers ─────────────────────────────────────────────────────────────


def _ok(data: dict) -> JSONResponse:
    return JSONResponse({"ok": True, **data})


def _err(msg: str, code: str = "error", status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "code": code}, status_code=status)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Render HTTP errors (401/403 from auth dependencies, 404 routing, ...)
    in the same {ok:false, error, code} JSON shape the UI already checks."""
    return JSONResponse(
        {"ok": False, "error": str(exc.detail), "code": "http_error"},
        status_code=exc.status_code,
        headers=exc.headers,
    )


def _safe_settings(row_map: dict) -> dict:
    """Public settings view — never expose secrets/PIN material to the UI."""
    secret_keys = {"pin_hash", "secret", "webhook_secret"}
    return {k: v for k, v in row_map.items() if k not in secret_keys}


# ── Auth ─────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})


@app.post("/login")
def login(request: Request, pin: str = Form(...), name: str = Form(default="Mwenyekiti")):
    """PIN login against the `users` table.

    When the system has no users yet this is first-time setup: the first
    account is created as *mwenyekiti* (chairperson / bootstrap admin), who
    can then create the treasurer (mhazinaji) and secretary (katibu).

    Success issues a random opaque session token in an HttpOnly cookie;
    failed attempts are rate limited per client IP.
    """
    ip = request.client.host if request.client else ""
    if auth.login_rate_limited(ip):
        return templates.TemplateResponse(
            request=request, name="login.html",
            status_code=429,
            context={
                "request": request,
                "error": "Majaribio mengi yameshindikana. Subiri dakika 10 kabla ya kujaribu tena.",
            },
        )

    conn = db.connect()
    try:
        if not conn.execute("SELECT 1 FROM users").fetchone():
            # First-time setup — the creator becomes the chairperson.
            display = name.strip() or "Mwenyekiti"
            cur = conn.execute(
                "INSERT INTO users(name, role, pin_hash) VALUES(?, 'mwenyekiti', ?)",
                (display, auth.hash_pin(pin)),
            )
            auth.audit(conn, cur.lastrowid, "login", f"First-run account created: {display}", ip)
            token = auth.create_session(conn, cur.lastrowid)
            conn.commit()
        else:
            user, needs_upgrade = auth.verify_login(conn, pin)
            if not user:
                auth.record_login_failure(ip)
                return templates.TemplateResponse(
                    request=request, name="login.html",
                    context={"request": request, "error": "PIN si sahihi. Jaribu tena."},
                )
            auth.clear_login_failures(ip)
            if needs_upgrade:
                auth.upgrade_legacy_pin(conn, user["id"], pin)
                auth.audit(conn, user["id"], "pin_rehash", "Legacy PIN hash upgraded to PBKDF2", ip)
            auth.audit(conn, user["id"], "login", "Successful PIN login", ip)
            token = auth.create_session(conn, user["id"])
            conn.commit()
    finally:
        conn.close()

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, max_age=86400 * 30,
        samesite="strict", secure=request.url.scheme == "https",
    )
    return response


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        conn = db.connect()
        try:
            auth.destroy_session(conn, token)
            conn.commit()
        finally:
            conn.close()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/api/me")
def api_me(user: dict = Depends(auth.get_current_user)):
    """Current user + role (drives UI visibility decisions)."""
    return _ok({
        "user": {
            "id": user["id"],
            "name": user["name"],
            "role": user["role"],
            "role_label": auth.role_label(user["role"]),
        }
    })


@app.post("/api/auth/change-pin")
def api_change_pin(request: Request, old_pin: str = Form(...), new_pin: str = Form(...)):
    """Change the *current* user's PIN (requires the old PIN).

    Every existing session of the user is revoked and a fresh session
    cookie is issued, so other devices are logged out.
    """
    user = auth.session_user(request)
    if not user:
        return _err("Ingia PIN kwanza.", "auth", status=401)
    if len(new_pin) < 4:
        return _err("PIN mpya iwe na tarakimu 4 na zaidi.", "invalid_pin")
    conn = db.connect()
    try:
        if not auth.verify_pin(user["pin_hash"], old_pin):
            return _err("PIN ya sasa si sahihi.", "bad_current_pin")
        conn.execute("UPDATE users SET pin_hash=? WHERE id=?", (auth.hash_pin(new_pin), user["id"]))
        auth.destroy_user_sessions(conn, user["id"])
        token = auth.create_session(conn, user["id"])
        auth.audit(conn, user["id"], "pin_changed", "PIN changed", request.client.host if request.client else "")
        conn.commit()
    finally:
        conn.close()
    response = JSONResponse({"ok": True, "message": "PIN imebadilishwa ✓"})
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, max_age=86400 * 30,
        samesite="strict", secure=request.url.scheme == "https",
    )
    return response


# ── Web UI ───────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"request": request, "llm": config.llm_enabled()},
    )


# ── Parse (phase 1: no side effects) ─────────────────────────────────────


@app.post("/parse")
async def parse_endpoint(
    request: Request,
    text: str = Form(...),
    user: dict = Depends(auth.get_current_user),
):
    """Parse Swahili text into a structured intent. No DB writes.

    Requires login: with an LLM provider configured every call costs money,
    so an open endpoint would be a cost-abuse vector."""
    try:
        if config.llm_enabled():
            from .llm_parser import parse as llm_parse
            intent = await llm_parse(text)
        else:
            intent = rule_parse(text)
        return _ok({"intent": asdict(intent)})
    except Exception as e:
        logger.exception("Parse failed")
        return _err(f"Hitilafu ya kupata maana: {e}", "parse_error")


# ── Commit (phase 2: atomic, deterministic) ──────────────────────────────


@app.post("/commit")
def commit_endpoint(
    request: Request,
    data: str = Form(...),
    user: dict = Depends(auth.get_treasurer),
):
    """Execute a parsed intent (treasurer or chairperson only).

    The frontend sends the intent JSON after the user taps [Thibitisha].
    Idempotency key prevents double execution."""
    actor = user["name"]

    try:
        payload = json.loads(data)
        intent = ParsedIntent(**{k: payload.get(k) for k in ParsedIntent.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError):
        return _err("Data batili: si JSON halali.", "invalid_data")

    idem_key = payload.get("idempotency_key") or secrets.token_hex(16)

    try:
        with db.transaction() as conn:
            receipt = _dispatch(conn, intent, actor, idem_key)
        return _ok(receipt)
    except DuplicateCommit as e:
        return _ok(e.receipt)  # already committed — return original receipt
    except AppError as e:
        return _err(e.message, e.code)
    except Exception:
        logger.exception("Commit failed for actor=%s", actor)
        return _err("Samahani, hitilafu ya ndani ya mfumo. Jaribu tena baadae.", "server_error", status=500)


def _dispatch(conn, intent: ParsedIntent, actor: str, idem_key: str) -> dict:
    action = intent.action
    ref = intent.mpesa_ref
    if action == "register":
        phone = (intent.amounts or {}).get("phone")
        return commit_register(conn, name=intent.member, phone=phone, actor=actor, idem_key=idem_key)
    elif action == "contribute":
        return commit_contribute(
            conn, member_name=intent.member, amounts=intent.amounts or {},
            actor=actor, idem_key=idem_key, mpesa_ref=ref,
        )
    elif action == "fee":
        return commit_fee(conn, amount=intent.amount, actor=actor, idem_key=idem_key)
    elif action == "fine":
        return commit_fine(
            conn, member_name=intent.member, amount=intent.amount,
            actor=actor, idem_key=idem_key,
        )
    elif action == "loan":
        return commit_loan(
            conn, member_name=intent.member, amount=intent.amount,
            guarantors=intent.guarantors, actor=actor, idem_key=idem_key,
        )
    elif action == "repay":
        return commit_repay(
            conn, member_name=intent.member, amount=intent.amount,
            actor=actor, idem_key=idem_key, mpesa_ref=ref,
        )
    elif action == "payout":
        return commit_payout(
            conn, member_name=intent.member, amount=intent.amount,
            actor=actor, idem_key=idem_key,
        )
    elif action == "expense":
        return commit_expense(
            conn, amount=intent.amount, description=intent.description,
            actor=actor, idem_key=idem_key,
        )
    elif action == "exit":
        return commit_exit(
            conn, member_name=intent.member, actor=actor, idem_key=idem_key,
        )
    else:
        raise AppError(f"Hatua '{action}' haijulikani. Jaribu: msajili, changia, kopa, lipa mkopo, faini, ada, kutoka, taarifa.")


# ── Read / Report endpoints (pure SELECTs) ──────────────────────────────


@app.get("/api/statement/{member_name}")
def api_statement(member_name: str, user: dict = Depends(auth.get_current_user)):
    try:
        conn = db.connect()
        try:
            result = member_statement(conn, member_name)
        finally:
            conn.close()
        return _ok(result)
    except AppError as e:
        return _err(e.message)


@app.get("/api/group")
def api_group(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        result = group_position(conn)
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/meeting")
def api_meeting(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        result = meeting_sheet(conn)
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/unpaid")
def api_unpaid(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        result = who_hasnt_paid_today(conn, {})
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/gawio")
def api_gawio(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        result = gawio_estimate(conn)
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/health")
def api_health():
    conn = db.connect()
    try:
        ledger.verify_invariant(conn)
        ok = True
        msg = "Mingatio ya mahesabu sawa ✓"
    except AppError as e:
        ok = False
        msg = str(e.message)
    finally:
        conn.close()
    return _ok({"invariant_ok": ok, "message": msg})


@app.get("/api/members")
def api_members(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, member_no, name, phone, join_date, status FROM members ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return _ok({"members": [dict(r) for r in rows]})


# ── CSV export (BSDA auditors) ──────────────────────────────────────────


@app.get("/api/export/meeting.csv")
def export_meeting_csv(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        sheet = meeting_sheet(conn)
    finally:
        conn.close()
    lines = ["ID,aina,maelezo,mwanachama,namba,debit,credit"]
    for e in sheet["entries"]:
        lines.append(
            f'{e["journal_id"]},{e["kind"]},"{e["description"]}",'
            f'{e["member"] or ""},{e["member_no"] or ""},'
            f'{e["debit"]},{e["credit"]}'
        )
    csv = "\n".join(lines)
    return Response(content=csv, media_type="text/csv; charset=utf-8")


@app.get("/api/exit/{member_name}")
def api_exit(member_name: str, user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        member = members.resolve(conn, member_name)
        result = members.exit_settlement(conn, member)
        return _ok(result)
    except AppError as e:
        return _err(e.message)
    finally:
        conn.close()

@app.get("/api/settings")
def api_get_settings(user: dict = Depends(auth.get_current_user)):
    conn = db.connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return _ok({"settings": _safe_settings({r["key"]: r["value"] for r in rows})})
    finally:
        conn.close()


@app.post("/api/settings")
def api_update_settings(
    request: Request,
    data: str = Form(...),
    user: dict = Depends(auth.get_admin),
):
    """Change group settings — chairperson (mwenyekiti) only."""
    try:
        payload = json.loads(data)
    except Exception:
        return _err("Data batili: si JSON halali.", status=400)

    conn = db.connect()
    try:
        for k, v in payload.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(k), str(v)),
            )
        auth.audit(
            conn, user["id"], "settings_change", f"Updated {sorted(payload)}",
            request.client.host if request.client else "",
        )
        conn.commit()
        return _ok({"message": "Mipangilio imesasishwa kikamilifu ✓"})
    finally:
        conn.close()


# ── User management (mwenyekiti only) ──────────────────────────────────────


@app.get("/api/admin/users")
def api_list_users(user: dict = Depends(auth.get_admin)):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, role, phone, is_active, created_at FROM users ORDER BY id"
        ).fetchall()
        return _ok({"users": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/admin/users")
def api_create_user(
    request: Request,
    name: str = Form(...),
    role: str = Form(...),
    phone: str = Form(default=""),
    user: dict = Depends(auth.get_admin),
):
    """Create a new committee member with an auto-generated temporary PIN."""
    role = role.strip().lower()
    if role not in auth.ROLES:
        return _err(f"Jukumu '{role}' halijulikani. Tumia: katibu, mhazinaji, mwenyekiti.", "invalid_role")
    name = name.strip()
    if not (2 <= len(name) <= 60):
        return _err("Jina liwe na herufi 2-60.", "invalid_name")

    temp_pin = f"{secrets.randbelow(10000):04d}"
    phone_clean = "".join(ch for ch in (phone or "") if ch.isdigit()) or None

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users(name, role, pin_hash, phone) VALUES(?, ?, ?, ?)",
            (name, role, auth.hash_pin(temp_pin), phone_clean),
        )
        auth.audit(
            conn, user["id"], "user_created", f"{name} ({role})",
            request.client.host if request.client else "",
        )
        conn.commit()
    finally:
        conn.close()
    return _ok({
        "message": f"{name} ameongezwa kama {auth.role_label(role)}.",
        "temp_pin": temp_pin,
        "warning": "Mwambie abadili PIN mara moja.",
    })


# ── WhatsApp webhook (OpenWA gateway) ─────────────────────────────────────
#
# Flow:  member sends Swahili command via WhatsApp → OpenWA `message.received`
#        → this endpoint → existing parse/commit engine → auto Swahili reply.

WHATSAPP_RESTRICTED = {"expense", "exit", "payout"}


async def _wa_read_only(chat_id: str, query, formatter, action_name: str = "read_only") -> JSONResponse:
    """Run a pure-SELECT report and reply; open to any WhatsApp sender."""
    try:
        conn = db.connect()
        try:
            data = query(conn)
        finally:
            conn.close()
        await wa_bridge.send_reply(chat_id, formatter(data))
        return _ok({"handled": True, "action": action_name})
    except AppError as e:
        await wa_bridge.send_reply(chat_id, f"⚠️ {e.message}")
        return _ok({"handled": True, "error": e.message})


@app.post("/api/webhook/whatsapp")
async def webhook_whatsapp(request: Request):
    """OpenWA gateway webhook — WhatsApp ⟷ VICOBA treasurer.

    OpenWA POSTs HMAC-signed ``message.received`` events here. The message
    body is treated as a Swahili VICOBA command, executed with the existing
    deterministic engine, and answered with a short Swahili receipt/statement.
    """
    raw = await request.body()

    secret = config.openwa_webhook_secret()
    if secret and not wa_bridge.verify_signature(
        raw, request.headers.get("X-OpenWA-Signature", ""), secret
    ):
        return _err("Sahihi ya WhatsApp si sahihi.", "bad_signature", status=401)

    try:
        data = json.loads(raw or b"{}")
    except Exception:
        return _err("Payload batili.", "invalid_payload", status=400)

    if data.get("event") != "message.received":
        # Ack session.status / message.ack / group.* untouched.
        return _ok({"handled": False, "event": data.get("event")})

    msg = data.get("data", {})
    text = (msg.get("body") or msg.get("text") or "").strip()
    chat_id = msg.get("from") or msg.get("chatId") or ""
    contact = msg.get("contact") or {}
    sender_phone = wa_bridge.chat_from_id(chat_id)

    if not text:
        await wa_bridge.send_reply(chat_id, wa_bridge.HELP_TEXT)
        return _ok({"handled": True, "action": "empty"})

    if config.llm_enabled():
        from .llm_parser import parse as llm_parse
        intent = await llm_parse(text)
    else:
        intent = rule_parse(text)

    # Read-only reports are open to any WhatsApp sender.
    if intent.action == "member_statement":
        return await _wa_read_only(
            chat_id,
            lambda conn: member_statement(conn, intent.member),
            wa_bridge.statement_text,
            action_name="member_statement",
        )
    if intent.action == "group_position":
        return await _wa_read_only(
            chat_id, group_position, wa_bridge.group_position_text,
            action_name="group_position",
        )
    if intent.action == "who_unpaid":
        return await _wa_read_only(
            chat_id, lambda conn: who_hasnt_paid_today(conn, {}), wa_bridge.unpaid_text,
            action_name="who_unpaid",
        )

    if intent.action == "unknown":
        await wa_bridge.send_reply(chat_id, wa_bridge.HELP_TEXT)
        return _ok({"handled": True, "action": "unknown"})

    # Sender identity — phone is the WhatsApp chat id without the @suffix.
    conn = db.connect()
    try:
        sender_member = wa_bridge.resolve_member_by_phone(conn, sender_phone)
        treasurer_in_users = auth.whatsapp_treasurer(conn, sender_phone)
    finally:
        conn.close()
    # Static env list remains a supported fallback for treasurer phones.
    is_treasurer = (sender_phone in config.openwa_treasurer_numbers()) or treasurer_in_users
    is_known = sender_member is not None

    # Private funds-moving / member-removal commands → treasurer (or chair) only.
    if intent.action in WHATSAPP_RESTRICTED and not is_treasurer:
        await wa_bridge.send_reply(chat_id, wa_bridge.auth_denied_text(intent.action))
        return _ok({"handled": True, "action": intent.action, "authorized": False})

    # Registration must come from a known member or the treasurer.
    if intent.action == "register" and not (is_treasurer or is_known):
        await wa_bridge.send_reply(chat_id, wa_bridge.auth_denied_text("register"))
        return _ok({"handled": True, "action": "register", "authorized": False})

    # Money-moving commands must come from a recognised member (or treasurer).
    if intent.action in ("contribute", "fee", "fine", "loan", "repay") and not (is_treasurer or is_known):
        await wa_bridge.send_reply(chat_id, wa_bridge.auth_denied_text("member"))
        return _ok({"handled": True, "action": intent.action, "authorized": False})

    # Self-registration: attach the sender's own number when none typed.
    if intent.action == "register":
        amounts = dict(intent.amounts or {})
        if not amounts.get("phone") and sender_phone:
            amounts["phone"] = wa_bridge.phone_digits(sender_phone)[-9:]
        intent.amounts = amounts

    # If the command didn't name anyone, attribute it to the sender.
    if intent.action in ("contribute", "repay", "fine", "loan", "exit", "payout") and not intent.member and sender_member:
        intent.member = sender_member["name"]

    actor = f"WhatsApp ({sender_phone or chat_id})"
    idem = data.get("idempotencyKey") or data.get("deliveryId") or ""
    idem_key = f"wa-{idem}" if idem else f"wa-{secrets.token_hex(8)}"

    try:
        with db.transaction() as conn:
            receipt = _dispatch(conn, intent, actor, idem_key)
        reply = wa_bridge.receipt_text(receipt)
    except DuplicateCommit as e:
        receipt = {**e.receipt, "duplicate": True}
        reply = wa_bridge.receipt_text(receipt)
    except AppError as e:
        await wa_bridge.send_reply(chat_id, f"⚠️ {e.message}")
        return _ok({"handled": True, "action": intent.action, "error": e.message})

    await wa_bridge.send_reply(chat_id, reply)
    return _ok({"handled": True, "action": intent.action, "receipt": receipt})


# ── Make.com / SMS webhook ───────────────────────────────────────────────
# Actions that move group funds out or remove members are deliberately refused
# here — they require an authenticated in-app login by a treasurer/admin.

MAKE_RESTRICTED = {"expense", "payout", "exit"}


@app.post("/api/webhook/make")
async def webhook_make(request: Request):
    """Webhook endpoint for Make.com / Tasker / SMS Gateway integration.

    Verifies the X-VICOBA-Secret header (set as the `webhook_secret` setting)
    and refuses outbound / member-removal actions to prevent wire fraud.
    Allows automated SMS parsing and transaction recording from M-Pesa SMS."""
    conn = db.connect()
    try:
        secret_ok = auth.webhook_secret_valid(conn, request.headers.get("x-vicoba-secret", ""))
    finally:
        conn.close()
    if not secret_ok:
        return _err("Siri ya webhook si sahihi.", "auth", status=401)

    try:
        data = await request.json()
    except Exception:
        try:
            form_data = await request.form()
            data = dict(form_data)
        except Exception:
            data = {}

    text = data.get("text") or data.get("message") or data.get("body")
    if not text:
        return _err("Hakuna ujumbe uliotumwa", "missing_text", status=400)

    sender = data.get("sender") or data.get("phone") or data.get("from")

    if config.llm_enabled():
        from .llm_parser import parse as llm_parse
        intent = await llm_parse(text)
    else:
        intent = rule_parse(text)

    if intent.action == "unknown":
        return _err(f"Haikuweza kutambua ujumbe: '{text}'", "unknown_intent", status=400)

    if intent.action in MAKE_RESTRICTED:
        return _err(
            "Kitendo hiki kihitaji kuingia mfumo moja kwa moja (Hazina/Mwenyekiti).",
            "forbidden", status=403,
        )

    # Auto-resolve member by phone if name missing in intent
    if not intent.member and sender:
        conn = db.connect()
        try:
            phone_clean = "".join(filter(str.isdigit, str(sender)))[-9:]
            if phone_clean:
                row = conn.execute("SELECT name FROM members WHERE phone LIKE ?", (f"%{phone_clean}",)).fetchone()
                if row:
                    intent.member = row["name"]
        finally:
            conn.close()

    actor = f"Make.com ({sender or 'Webhook'})"
    idem_key = intent.mpesa_ref or f"make-{secrets.token_hex(8)}"

    try:
        with db.transaction() as conn:
            receipt = _dispatch(conn, intent, actor, idem_key)
        return _ok({"received": text, "receipt": receipt})
    except DuplicateCommit as e:
        return _ok({"received": text, "receipt": e.receipt, "duplicate": True})
    except AppError as e:
        return _err(e.message, e.code)
    except Exception:
        logger.exception("Make webhook commit failed")
        return _err("Samahani, hitilafu ya ndani ya mfumo. Jaribu tena baadae.", "server_error", status=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
