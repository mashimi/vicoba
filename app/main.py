"""VICOBA AI Digital Treasurer — FastAPI application.

Two-phase architecture (from the review's Section 7):
  1. POST /parse  → rule-based (or LLM) parser returns structured intent
  2. POST /commit → deterministic Python executes the validated intent atomically

Authentication: a simple treasurer PIN stored in settings. Every mutation
is logged with the actor identity.
"""
import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Form, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, ledger
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

app = FastAPI(title="VICOBA Digital Treasurer")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


# ── Helpers ─────────────────────────────────────────────────────────────


def _actor(request: Request) -> str:
    """Return the authenticated actor name from the session cookie."""
    pin = request.cookies.get("vicoba_pin")
    if not pin:
        raise HTTPException(status_code=401, detail="Hakuna uhakika. Ingia PIN kwanza.")
    conn = db.connect()
    try:
        stored = db.get_setting(conn, "pin_hash", "")
        if not stored or not hmac.compare_digest(pin, stored):
            raise HTTPException(status_code=401, detail="PIN si sahihi. Jaribu tena.")
        return db.get_setting(conn, "treasurer_name", "Mhazinaji")
    finally:
        conn.close()


def _actor_from_cookie(request: Request) -> str:
    """Like _actor but for non-exception paths (returns 'unknown' if no auth)."""
    pin = request.cookies.get("vicoba_pin")
    if not pin:
        return "unknown"
    conn = db.connect()
    try:
        stored = db.get_setting(conn, "pin_hash", "")
        if stored and hmac.compare_digest(pin, stored):
            return db.get_setting(conn, "treasurer_name", "Mhazinaji")
    finally:
        conn.close()
    return "unknown"


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def _ok(data: dict) -> JSONResponse:
    return JSONResponse({"ok": True, **data})


def _err(msg: str, code: str = "error", status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "code": code}, status_code=status)


# ── Startup ─────────────────────────────────────────────────────────────


@app.on_event("startup")
def startup():
    db.init_db()


# ── Auth ─────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login(request: Request, pin: str = Form(...), name: str = Form(default="Mhazinaji")):
    conn = db.connect()
    try:
        stored = db.get_setting(conn, "pin_hash", "")
        if not stored:
            # First-time setup: this PIN becomes the treasurer PIN
            hashed = _hash_pin(pin)
            conn.execute("INSERT INTO settings(key, value) VALUES('pin_hash', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (hashed,))
            conn.execute("INSERT INTO settings(key, value) VALUES('treasurer_name', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name.strip(),))
            conn.commit()
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie("vicoba_pin", hashed, httponly=True, max_age=86400 * 30, samesite="strict")
            return response
        if hmac.compare_digest(_hash_pin(pin), stored):
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie("vicoba_pin", _hash_pin(pin), httponly=True, max_age=86400 * 30, samesite="strict")
            return response
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "PIN si sahihi. Jaribu tena."}
        )
    finally:
        conn.close()


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("vicoba_pin")
    return response


# ── Web UI ───────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "llm": config.llm_enabled()})


# ── Parse (phase 1: no side effects) ─────────────────────────────────────


@app.post("/parse")
async def parse_endpoint(request: Request, text: str = Form(...)):
    """Parse Swahili text into a structured intent. No DB writes."""
    try:
        if config.llm_enabled():
            from .llm_parser import parse as llm_parse
            intent = await llm_parse(text)
        else:
            intent = rule_parse(text)
        return _ok({"intent": asdict(intent)})
    except Exception as e:
        return _err(f"Hitilafu ya kupata maana: {e}", "parse_error")


# ── Commit (phase 2: atomic, deterministic) ──────────────────────────────


@app.post("/commit")
def commit_endpoint(request: Request, data: str = Form(...)):
    """Execute a parsed intent. The frontend sends the intent JSON after
    the user taps [Thibitisha]. Idempotency key prevents double execution."""
    try:
        actor = _actor(request)
    except HTTPException as e:
        return _err(e.detail, "auth", status=401)

    try:
        payload = json.loads(data)
        intent = ParsedIntent(**{k: payload.get(k) for k in ParsedIntent.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError) as e:
        return _err(f"Data batili: {e}", "invalid_data")

    idem_key = payload.get("idempotency_key") or secrets.token_hex(16)

    try:
        with db.transaction() as conn:
            receipt = _dispatch(conn, intent, actor, idem_key)
        return _ok(receipt)
    except DuplicateCommit as e:
        return _ok(e.receipt)  # already committed — return original receipt
    except AppError as e:
        return _err(e.message, e.code)
    except Exception as e:
        return _err(f"Hitilafu: {e}", "server_error")


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
def api_statement(member_name: str):
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
def api_group():
    conn = db.connect()
    try:
        result = group_position(conn)
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/meeting")
def api_meeting():
    conn = db.connect()
    try:
        result = meeting_sheet(conn)
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/unpaid")
def api_unpaid():
    conn = db.connect()
    try:
        result = who_hasnt_paid_today(conn, {})
    finally:
        conn.close()
    return _ok(result)


@app.get("/api/gawio")
def api_gawio():
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
def api_members():
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
def export_meeting_csv():
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
def api_exit(member_name: str):
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
def api_get_settings():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return _ok({"settings": {r["key"]: r["value"] for r in rows}})
    finally:
        conn.close()


@app.post("/api/settings")
def api_update_settings(request: Request, data: str = Form(...)):
    try:
        actor = _actor(request)
    except HTTPException as e:
        return _err(e.detail, "auth", status=401)

    try:
        payload = json.loads(data)
    except Exception as e:
        return _err(f"Data batili: {e}")

    conn = db.connect()
    try:
        for k, v in payload.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(k), str(v)),
            )
        conn.commit()
        return _ok({"message": "Mipangilio imesasishwa kikamilifu ✓"})
    finally:
        conn.close()


@app.post("/api/webhook/make")
async def webhook_make(request: Request):
    """Webhook endpoint for Make.com / Tasker / SMS Gateway integration.
    Allows automated SMS parsing and transaction recording from M-Pesa notifications."""
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
    except Exception as e:
        return _err(f"Hitilafu: {e}", "server_error")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
