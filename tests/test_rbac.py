"""RBAC tests — 3-tier committee model (Mwenyekiti > Mhazinaji > Katibu).

Covers: bootstrap admin, role-gated endpoints, user management with temp PIN,
webhook secret for /api/webhook/make, PIN change, audit log, and secret
redaction from the public settings endpoint.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import SCHEMA, connect
from app.ledger import ensure_account
from app.main import app


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    monkeypatch.setenv("GROUP_NAME", "Test VICOBA")
    monkeypatch.setenv("USE_LLM_PARSER", "0")
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
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _login(client, pin, name="Mwenyekiti"):
    return client.post("/login", data={"pin": pin, "name": name}, follow_redirects=False)


def _commit(client, text):
    """Parse then commit a Swahili command (as the current session user)."""
    parse = client.post("/parse", data={"text": text})
    intent = parse.json()["intent"]
    return client.post("/commit", data={"data": json.dumps(intent)})


# ── Bootstrap ─────────────────────────────────────────────────────────────


def test_first_login_creates_mwenyekiti(client):
    res = _login(client, "1234", name="Baba Chair")
    assert res.status_code == 303
    me = client.get("/api/me").json()
    assert me["ok"]
    assert me["user"]["name"] == "Baba Chair"
    assert me["user"]["role"] == "mwenyekiti"


def test_second_login_with_wrong_pin_rejected(client):
    _login(client, "1111")
    res = _login(client, "9999")
    assert res.status_code == 200  # re-rendered login page
    assert "PIN si sahihi" in res.text


def test_api_me_requires_auth(client):
    res = client.get("/api/me")
    assert res.status_code == 401


def test_login_writes_audit(client):
    _login(client, "1234")
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='login'").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


# ── Role gating ───────────────────────────────────────────────────────────


def test_katibu_cannot_commit(client):
    _login(client, "1234")  # mwenyekiti
    resp = client.post(
        "/api/admin/users",
        data={"name": "Dina Katibu", "role": "katibu", "phone": ""},
    )
    assert resp.status_code == 200
    temp_pin = resp.json()["temp_pin"]
    assert len(temp_pin) == 4

    _login(client, temp_pin, "Dina Katibu")
    me = client.get("/api/me").json()
    assert me["user"]["role"] == "katibu"

    # Secretary cannot execute transactions
    commit_res = _commit(client, "msajili Juma 0712345678")
    assert commit_res.status_code == 403


def test_treasurer_can_commit_but_not_change_settings(client):
    _login(client, "1234")  # mwenyekiti
    resp = client.post(
        "/api/admin/users",
        data={"name": "Hazina", "role": "mhazinaji", "phone": "0712345678"},
    )
    temp_pin = resp.json()["temp_pin"]
    assert len(temp_pin) == 4

    _login(client, temp_pin, "Hazina")
    me = client.get("/api/me").json()
    assert me["user"]["role"] == "mhazinaji"

    # Treasurer can commit money (a group fee needs no member registration)
    commit_res = _commit(client, "ada ya kikundi 2000")
    assert commit_res.status_code == 200
    assert commit_res.json()["ok"]

    # But cannot change group settings (admin-only)
    settings_res = client.post(
        "/api/settings", data={"data": json.dumps({"group_name": "Hacked"})}
    )
    assert settings_res.status_code == 403


def test_admin_changes_settings_and_writes_audit(client):
    _login(client, "1234")
    res = client.post(
        "/api/settings", data={"data": json.dumps({"group_name": "Kikundi A"})}
    )
    assert res.status_code == 200
    assert res.json()["ok"]

    conn = connect()
    try:
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action='settings_change' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    assert row is not None and "group_name" in row["detail"]
    assert n_users == 1
# ── Settings secrecy ────────────────────────────────────────────────────────


def test_public_settings_never_expose_secrets(client):
    _login(client, "1234")
    conn = connect()
    try:
        for k in ("webhook_secret", "secret", "pin_hash", "treasurer_name"):
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, "s3cr3t-value")
            )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/settings").json()
    assert data["ok"]
    for k in ("webhook_secret", "secret", "pin_hash"):
        assert k not in data["settings"]


# ── Webhook secret & restricted actions ────────────────────────────────────


def _set_webhook_secret(value):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('webhook_secret', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()


def test_webhook_make_requires_secret(client):
    _set_webhook_secret("sekrit-123")

    # Missing header → 401
    no_auth = client.post("/api/webhook/make", json={"text": "Juma amelipa hisa 5000"})
    assert no_auth.status_code == 401

    # Wrong header → 401
    bad = client.post(
        "/api/webhook/make",
        json={"text": "Juma amelipa hisa 5000"},
        headers={"X-VICOBA-Secret": "wrong"},
    )
    assert bad.status_code == 401

    # Correct header → allowed (parse succeeds for a known fee intent)
    ok = client.post(
        "/api/webhook/make",
        json={"text": "ada ya kikundi 2000", "sender": "0712345678"},
        headers={"X-VICOBA-Secret": "sekrit-123"},
    )
    assert ok.status_code == 200


def test_webhook_make_refuses_expense(client):
    _set_webhook_secret("sekrit-123")
    res = client.post(
        "/api/webhook/make",
        json={"text": "tulitumia 5000 vifaa", "sender": "0712345678"},
        headers={"X-VICOBA-Secret": "sekrit-123"},
    )
    assert res.status_code == 403
    assert res.json()["code"] == "forbidden"


# ── Change PIN ─────────────────────────────────────────────────────────────


def test_change_pin_flow(client):
    _login(client, "1234")
    res = client.post(
        "/api/auth/change-pin",
        data={"old_pin": "1234", "new_pin": "5678"},
    )
    assert res.status_code == 200
    assert res.json()["ok"]

    # Old PIN no longer works
    old = _login(client, "1234")
    assert "PIN si sahihi" in old.text

    # New PIN works
    new = _login(client, "5678")
    assert new.status_code == 303
    assert client.get("/api/me").json()["ok"]


def test_change_pin_requires_correct_old(client):
    _login(client, "1234")
    res = client.post(
        "/api/auth/change-pin",
        data={"old_pin": "0000", "new_pin": "5678"},
    )
    assert res.status_code == 400
    assert res.json()["ok"] is False
    assert res.json()["code"] == "bad_current_pin"