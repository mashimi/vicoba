"""Security tests: endpoint auth, session tokens, PIN hashing upgrade,
and login rate limiting."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.db import connect, SCHEMA
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
    ensure_account(conn, "cash", "Cash", "asset")
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


# Every data-bearing read must refuse anonymous callers (401, JSON body).
PROTECTED_GETS = [
    "/api/group",
    "/api/meeting",
    "/api/unpaid",
    "/api/gawio",
    "/api/members",
    "/api/settings",
    "/api/export/meeting.csv",
    "/api/statement/Amina",
    "/api/exit/Amina",
]


@pytest.mark.parametrize("url", PROTECTED_GETS)
def test_reads_require_login(client, url):
    res = client.get(url)
    assert res.status_code == 401
    data = res.json()
    assert data["ok"] is False
    assert "error" in data


def test_parse_requires_login(client):
    res = client.post("/parse", data={"text": "Amina amelipa hisa 5000"})
    assert res.status_code == 401
    assert res.json()["ok"] is False


def test_session_cookie_is_random_token_not_pin_hash(client):
    res = client.post("/login", data={"pin": "1234", "name": "Mhazinaji"}, follow_redirects=False)
    assert res.status_code == 303
    cookie = client.cookies.get(auth.SESSION_COOKIE)
    assert cookie
    # The cookie must NOT be the SHA-256 of the PIN (that was the old bug:
    # it matched the pin_hash column, so a DB leak yielded live sessions).
    assert cookie != hashlib.sha256(b"1234").hexdigest()
    assert client.get("/api/me").json()["ok"]


def test_logout_destroys_session_server_side(client):
    client.post("/login", data={"pin": "1234"}, follow_redirects=False)
    token = client.cookies.get(auth.SESSION_COOKIE)
    client.get("/logout", follow_redirects=False)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_legacy_plain_sha256_pin_upgraded_on_login(client):
    legacy = hashlib.sha256(b"4321").hexdigest()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users(name, role, pin_hash) VALUES('Zamani', 'mhazinaji', ?)",
            (legacy,),
        )
        conn.commit()
    finally:
        conn.close()

    res = client.post("/login", data={"pin": "4321"}, follow_redirects=False)
    assert res.status_code == 303

    conn = connect()
    try:
        stored = conn.execute(
            "SELECT pin_hash FROM users WHERE name='Zamani'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_pin(stored, "4321")


def test_new_pins_are_stored_as_pbkdf2(client):
    client.post("/login", data={"pin": "1234"}, follow_redirects=False)
    conn = connect()
    try:
        stored = conn.execute("SELECT pin_hash FROM users").fetchone()[0]
    finally:
        conn.close()
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_pin(stored, "1234")


def test_rate_limit_after_repeated_failures(client):
    client.post("/login", data={"pin": "1234"}, follow_redirects=False)
    client.get("/logout", follow_redirects=False)

    for _ in range(5):
        res = client.post("/login", data={"pin": "0000"}, follow_redirects=False)
        assert res.status_code == 200  # re-rendered login page

    res = client.post("/login", data={"pin": "0000"}, follow_redirects=False)
    assert res.status_code == 429

    # A successful login resets the counter (test-level reset mirrors it).
    auth.clear_login_failures("testclient")
    res = client.post("/login", data={"pin": "1234"}, follow_redirects=False)
    assert res.status_code == 303