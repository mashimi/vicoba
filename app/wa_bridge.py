"""WhatsApp bridge — OpenWA gateway ⟷ VICOBA treasurer.

A dedicated integration layer for the OpenWA WhatsApp API Gateway
(https://github.com/rmyndharis/OpenWA, NestJS). Members chat a Swahili
VICOBA command (e.g. "Amina amelipa hisa 5000") to the group; OpenWA POSTs an
HMAC-signed ``message.received`` webhook to our ``/api/webhook/whatsapp``
route; we execute it with the existing deterministic parse/commit engine and
auto-reply with a short Swahili receipt/statement through OpenWA's
``/messages/send-text`` endpoint.

All OpenWA-specific concerns (signature format, payload shape, reply API)
live here so ``app/main.py`` only wires an HTTP route.
"""
import hashlib
import hmac
import logging
import re
import sqlite3
from typing import Optional
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("vicoba.wa")


def tsh(amount) -> str:
    """Money is integer shillings everywhere — never REAL."""
    return f"TZS {int(amount or 0):,}"


# ── Signature verification (OpenWA: `sha256=` + HMAC-SHA256 of raw body) ──


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify OpenWA's ``X-OpenWA-Signature: sha256=<hex>`` header.

    Computed over the *exact* bytes that were POSTed using the shared
    webhook secret. Constant-time comparison prevents timing attacks.
    """
    if not secret:
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature.lower(), expected)


# ── Chat / member helpers ────────────────────────────────────────────────


def chat_from_id(chat_id: str) -> str:
    """Strip the WhatsApp suffix: '628123456789@c.us' → '628123456789'."""
    return (chat_id or "").split("@")[0]


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def resolve_member_by_phone(conn: sqlite3.Connection, phone: str) -> Optional[sqlite3.Row]:
    """Find a registered member by WhatsApp number.

    WhatsApp numbers arrive in international form (255712345678) while
    members are registered with the local form (0712345678). A fuzzy match on
    the last 9 digits handles both, mirroring the existing Make.com webhook.
    """
    digits = phone_digits(phone)
    if not digits:
        return None
    suffix = digits[-9:]
    return conn.execute(
        "SELECT * FROM members WHERE phone LIKE ?", (f"%{suffix}",)
    ).fetchone()


# ── OpenWA outbound send (best-effort — never fails the HTTP response) ────


async def send_reply(chat_id: str, text: str) -> bool:
    """Send a WhatsApp message back to a chat via OpenWA.

    Failure-tolerant: if OpenWA is down or unconfigured we log and return
    False so the webhook still acks (OpenWA will retry on non-2xx anyway).
    """
    base = config.openwa_url().rstrip("/")
    api_key = config.openwa_api_key()
    session = config.openwa_session_id()
    if not api_key or not session:
        log.warning("OpenWA not configured (OPENWA_API_KEY / OPENWA_SESSION_ID); reply skipped.")
        return False
    url = f"{base}/api/sessions/{quote(session, safe='')}/messages/send-text"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"chatId": chat_id, "text": text},
            )
            resp.raise_for_status()
        return True
    except Exception as e:  # pragma: no cover - IO/network path
        log.error("Failed to send WhatsApp reply: %s", e)
        return False
# ── Swahili reply formatting ───────────────────────────────────────────────


def receipt_text(receipt: dict) -> str:
    """Mobile-friendly receipt for any commit."""
    group = config.group_name() or "VICOBA"
    lines = [f"🧾 RASITI — {group}"]
    msg = receipt.get("message") or ""
    # commit messages already carry the ✅; the header owns the emoji
    lines.append(msg.lstrip("✅ ").strip())
    if receipt.get("journal_id"):
        lines.append(f"Namba: JN-{receipt['journal_id']}")
    if receipt.get("mpesa_ref"):
        lines.append(f"Ref M-Pesa: {receipt['mpesa_ref']}")
    if receipt.get("remaining") is not None:
        lines.append(f"Baki ya mkopo: {tsh(receipt['remaining'])}")
    if receipt.get("duplicate"):
        lines.append("(Imerekodiwa tayari — kurudia hakuna madhara)")
    return "\n".join(lines)


def statement_text(stmt: dict) -> str:
    """Swahili member statement — balances + last few transactions."""
    lines = [
        f"👤 {stmt.get('name', '')} ({stmt.get('member_no', '')})",
        f"💰 Hisa: {tsh(stmt.get('hisa'))}",
        f"💳 Akiba: {tsh(stmt.get('akiba'))}",
        f"📌 Deni la mkopo: {tsh(stmt.get('deni_lichangiwa'))}",
        f"⚖️ Jumla (Hisa+Akiba): {tsh(stmt.get('equity'))}",
        "—",
    ]
    for txn in (stmt.get("transactions") or [])[:5]:
        lines.append(
            f"• {txn.get('tx_date', '')} {str(txn.get('kind', '')).replace('_', ' ')}: "
            f"{str(txn.get('description', ''))[:52]}"
        )
    return "\n".join(lines)


def group_position_text(pos: dict) -> str:
    """Swahili group snapshot."""
    group = config.group_name() or "Kikundi cha VICOBA"
    return "\n".join([
        f"📊 {group}",
        f"💰 Fedha tasani: {tsh(pos.get('cash'))}",
        f"👥 Wanachama walio hai: {pos.get('active_members')}/{pos.get('total_members')}",
        f"🏦 Mikopo hai: {pos.get('active_loans')} — deni {tsh(pos.get('total_outstanding_loans'))}",
        f"🏰 Jamii: {tsh(pos.get('jamii'))} | Bima: {tsh(pos.get('bima'))}",
    ])


def unpaid_text(data: dict) -> str:
    """Swahili list of members who have not contributed today."""
    missing = data.get("missing") or []
    lines = [f"📋 Wasiolipa leo: {len(missing)}/{data.get('total_active')}"]
    for m in missing:
        lines.append(f"• {m.get('name', '')} ({m.get('member_no', '')})")
    if not missing:
        lines.append("Wote wamelipa! 🎉")
    return "\n".join(lines)


HELP_TEXT = (
    "🔤 Siwezi kuelewa hilo. Jaribu moja kati ya:\n"
    "• 'Juma amelipa hisa 5000, jamii 1000'\n"
    "• 'Juma amelipa mkopo 10000'\n"
    "• 'Ada ya kikundi 2000'\n"
    "• 'Faini ya Juma 1000'\n"
    "• 'Taarifa ya Juma' / 'Salio la Juma'\n"
    "• 'Maendeleo ya kikundi'\n"
    "• 'Nani hajalipa leo?'\n"
)


def auth_denied_text(action: str) -> str:
    """Explain why a privileged command was refused."""
    if action in ("expense", "exit", "payout"):
        return ("🔒 Kitendo hiki (matumizi/kutoka/mfuko) kinahitaji "
                "namba ya Mhazinaji kwenye WhatsApp.\nWasiliana na mhazinaji.")
    return (
        "🙅 Namba hii haijatambuliwa kama mwanachama.\n"
        "Iwapo ni namba yako, mhazinaji asajili namba hiyo kwenye kikundi, "
        "kisha jaribu tena.\nKwa mfano: 'msajili Juma 0712345678'."
    )