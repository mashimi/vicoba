"""WhatsApp bridge (OpenWA webhook) integration tests.

Covers the full flow: OpenWA `message.received` → HMAC verification →
existing parse/commit engine → auto Swahili reply (mocked to avoid network).
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import config, members, wa_bridge
from app.db import SCHEMA, connect
from app.ledger import ensure_account
from app.main import app


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def openwa_payload(text, phone="255712345678", *, idem="msg_abc_123", event="message.received"):
    return {
        "event": event,
        "timestamp": "2026-08-24T10:00:00.000Z",
        "sessionId": "sess_1",
        "idempotencyKey": idem,
        "deliveryId": "del_1",
        "data": {
            "id": "true_628123456789@c.us_3EB0ABC123",
            "from": f"{phone}@c.us",
            "to": "628987654321@g.us",
            "body": text,
            "type": "text",
            "waTimestamp": 1706868000,
            "isGroup": False,
        },
    }


def post_webhook(client, payload, secret="test-secret"):
    """POST an OpenWA webhook with a properly signed body."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post(
        "/api/webhook/whatsapp",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-OpenWA-Signature": sign(raw, secret),
            "X-OpenWA-Event": payload.get("event", "message.received"),
            "X-OpenWA-Idempotency-Key": payload.get("idempotencyKey", ""),
        },
    )


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    monkeypatch.setenv("GROUP_NAME", "Test VICOBA")
    monkeypatch.setenv("USE_LLM_PARSER", "0")
    monkeypatch.setenv("OPENWA_WEBHOOK_SECRET", "test-secret")
    monkeypatch.delenv("OPENWA_TREASURER_NUMBERS", raising=False)
    config.BASE_DIR = tmp_path

    conn = connect()
    conn.executescript(SCHEMA)
    for code, name, type_ in [
        ("cash", "Cash", "asset"),
        ("jamii", "Jamii", "liability"),
        ("bima", "Bima", "liability"),
        ("income:ada", "Ada", "income"),
        ("income:faini", "Faini", "income"),
        ("income:riba", "Riba", "income"),
        ("expense:matumizi", "Matumizi", "expense"),
    ]:
        ensure_account(conn, code, name, type_)
    for k, v in [
        ("interest_rate_pct", "10"), ("loan_weeks", "12"),
        ("eligibility_multiple", "3"), ("require_guarantors", "0"),
    ]:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT DO NOTHING", (k, v)
        )
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def replies(monkeypatch):
    """Capture auto-replies instead of hitting the real OpenWA server."""
    captured = []

    async def fake_send(chat_id: str, text: str) -> bool:
        captured.append({"chat_id": chat_id, "text": text})
        return True

    monkeypatch.setattr(wa_bridge, "send_reply", fake_send)
    return captured


def _register_member(name: str = "Juma Ally", phone: str = "0712345678"):
    conn = connect()
    try:
        member = members.register(conn, name, phone=phone)
        conn.commit()
        return member
    finally:
        conn.close()


# ── Signature verification ────────────────────────────────────────────────


def test_verify_signature_accepts_and_rejects():
    body = json.dumps({"hello": "world"}).encode("utf-8")
    good = sign(body, "sekrit")
    assert wa_bridge.verify_signature(body, good, "sekrit") is True
    assert wa_bridge.verify_signature(body, good.upper(), "sekrit") is True  # case-insensitive
    assert wa_bridge.verify_signature(body, good[:-2] + "00", "sekrit") is False
    assert wa_bridge.verify_signature(body, "", "sekrit") is False
    assert wa_bridge.verify_signature(body, "sha256=xx", "sekrit") is False
    assert wa_bridge.verify_signature(body, good, "") is True  # no secret -> open


def test_webhook_rejects_bad_signature(client, replies):
    raw = json.dumps(openwa_payload("Juma amelipa hisa 5000")).encode("utf-8")
    resp = client.post(
        "/api/webhook/whatsapp",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-OpenWA-Signature": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["ok"] is False
    assert replies == []


def test_webhook_ignores_non_message_events(client, replies):
    resp = post_webhook(client, openwa_payload("", event="session.status"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] and data["handled"] is False
    assert replies == []
# ── Full contribution flow: webhook → parse → commit → reply ─────────────


def test_contribute_full_flow(client, replies):
    _register_member("Juma", "0712345678")  # message says "Juma" — register that exact name
    resp = post_webhook(client, openwa_payload("Juma amelipa hisa 5000, jamii 1000"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] and data["handled"] is True
    assert data["action"] == "contribute"
    assert data["receipt"]["member_name"] == "Juma"

    # Auto-reply was captured
    assert len(replies) == 1
    text = replies[0]["text"]
    assert replies[0]["chat_id"] == "255712345678@c.us"
    assert "RASITI" in text
    assert "5,000" in text or "5000" in text
    assert "JN-" in text

    # Ledger actually moved
    conn = connect()
    try:
        row = conn.execute("SELECT kind, mpesa_ref FROM journals WHERE kind='contribute'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_duplicate_webhook_is_idempotent(client, replies):
    _register_member("Amina", "0711111111")  # exact name in the message
    p1 = openwa_payload("Amina amelipa hisa 5000", idem="msg_dup_1", phone="255711111111")
    r1 = post_webhook(client, p1)
    r2 = post_webhook(client, p1)  # same OpenWA idempotencyKey -> duplicate
    assert r1.json()["ok"] and r1.json()["handled"] is True
    assert r2.json()["ok"] and r2.json()["handled"] is True
    assert r2.json()["receipt"]["duplicate"] is True

    # Only one journal, one contribution
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM journals WHERE kind='contribute'").fetchone()[0]
        hisa = conn.execute(
            "SELECT COALESCE(SUM(credit), 0) FROM journal_lines WHERE account_code='hisa:1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert hisa == 5000  # not 10000 — the duplicate was rejected

    # Two replies (one per webhook call)
    assert len(replies) == 2
    assert "kurudia" in replies[1]["text"]


def test_statement_reply(client, replies):
    _register_member("Zawadi", "0712121212")
    # sender number = the member's own phone (international form)
    post_webhook(client, openwa_payload("Zawadi amelipa hisa 10000", phone="255712121212"))
    replies.clear()

    resp = post_webhook(client, openwa_payload("Taarifa ya Zawadi", phone="255712121212"))
    assert resp.status_code == 200
    assert resp.json()["handled"] is True
    assert resp.json()["action"] == "member_statement"
    text = replies[0]["text"]
    assert "Hisa:" in text
    assert "10,000" in text or "10000" in text
    assert "Zawadi" in text
    assert "Deni la mkopo" in text


def test_group_position_reply(client, replies):
    _register_member("Juma")
    resp = post_webhook(client, openwa_payload("Maendeleo ya kikundi"))
    assert resp.json()["handled"] is True
    assert resp.json()["action"] == "group_position"
    text = replies[0]["text"]
    assert "Fedha tasani" in text
    assert "Wanachama" in text


def test_unpaid_reply(client, replies):
    _register_member("Juma")
    resp = post_webhook(client, openwa_payload("Nani hajalipa leo?"))
    assert resp.json()["handled"] is True
    assert resp.json()["action"] == "who_unpaid"
    text = replies[0]["text"]
    assert "Wasiolipa" in text
    assert "Juma" in text


# ── Permissions ───────────────────────────────────────────────────────────


def test_unknown_number_cannot_contribute(client, replies):
    # No member registered with this phone -> not the treasurer either
    resp = post_webhook(client, openwa_payload("Amina amelipa hisa 5000"))
    assert resp.json()["handled"] is True
    assert resp.json()["authorized"] is False
    assert "haijatambuliwa" in replies[0]["text"] or "Namba" in replies[0]["text"]


def test_expense_requires_treasurer(client, replies, monkeypatch):
    # Member whose phone is NOT the treasurer → expense refused
    _register_member("Juma", "0712345678")
    resp = post_webhook(
        client, openwa_payload("Matumizi 5000", phone="255719999999")
    )  # not the treasurer
    assert resp.json()["authorized"] is False
    assert "Mhazinaji" in replies[0]["text"]

    # The treasurer's number (matches OPENWA_TREASURER_NUMBERS) can expense
    monkeypatch.setenv("OPENWA_TREASURER_NUMBERS", "255719999999")
    resp2 = post_webhook(
        client, openwa_payload("Matumizi 5000", phone="255719999999")
    )
    assert resp2.json()["handled"] is True
    assert resp2.json()["action"] == "expense"


def test_fee_by_registered_member(client, replies):
    _register_member("Amina", "0712345678")
    resp = post_webhook(client, openwa_payload("Ada ya kikundi 2000"))
    assert resp.json()["handled"] is True
    assert resp.json()["action"] == "fee"
    assert "RASITI" in replies[0]["text"]


# ── Unknown intent / help ─────────────────────────────────────────────────


def test_unknown_intent_returns_help(client, replies):
    resp = post_webhook(client, openwa_payload("habari za leo?"))
    assert resp.json()["handled"] is True
    assert resp.json()["action"] == "unknown"
    assert "Jaribu" in replies[0]["text"]