"""API Endpoints Integration Tests using FastAPI TestClient.
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import connect, SCHEMA
from app.ledger import ensure_account
from app.main import app


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    monkeypatch.setenv("GROUP_NAME", "Test VICOBA")
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
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT DO NOTHING", (k, v))
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def test_login_and_logout(client):
    # First time login sets pin
    res = client.post("/login", data={"pin": "1234", "name": "Mhazinaji Test"}, follow_redirects=False)
    assert res.status_code == 303
    assert "vicoba_pin" in client.cookies

    # Logout removes cookie
    res = client.get("/logout", follow_redirects=False)
    assert res.status_code == 303
    assert "vicoba_pin" not in client.cookies


def test_parse_endpoint(client):
    res = client.post("/parse", data={"text": "Amina amelipa hisa 5000, jamii 1000"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"]
    assert data["intent"]["action"] == "contribute"
    assert data["intent"]["member"] == "Amina"


def test_commit_flow_with_auth(client):
    # Login first
    login_res = client.post("/login", data={"pin": "1234", "name": "Mhazinaji Test"}, follow_redirects=False)
    assert login_res.status_code == 303

    # Register via commit
    parse_res = client.post("/parse", data={"text": "msajili Juma Ally 0712345678"})
    intent = parse_res.json()["intent"]

    commit_res = client.post("/commit", data={"data": __import__("json").dumps(intent)})
    assert commit_res.status_code == 200
    assert commit_res.json()["ok"]

    # Check members endpoint
    mem_res = client.get("/api/members")
    assert mem_res.status_code == 200
    members_data = mem_res.json()
    assert len(members_data["members"]) == 1
    assert members_data["members"][0]["name"] == "Juma Ally"


def test_health_endpoint(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"]
    assert data["invariant_ok"]


def test_export_meeting_csv(client):
    res = client.get("/api/export/meeting.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")


def test_settings_api(client):
    # Login first
    client.post("/login", data={"pin": "1234", "name": "Mhazinaji Test"}, follow_redirects=False)

    # Get settings
    res = client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"]

    # Update settings
    update_res = client.post(
        "/api/settings",
        data={"data": __import__("json").dumps({"mpesa_till": "554433", "mpesa_name": "VICOBA GROUP"})},
    )
    assert update_res.status_code == 200
    assert update_res.json()["ok"]

    # Verify updated settings
    res2 = client.get("/api/settings")
    assert res2.json()["settings"]["mpesa_till"] == "554433"
    assert res2.json()["settings"]["mpesa_name"] == "VICOBA GROUP"


def test_make_webhook_integration(client):
    # Register Juma first
    client.post("/login", data={"pin": "1234", "name": "Mhazinaji Test"}, follow_redirects=False)
    p = client.post("/parse", data={"text": "msajili Juma Ally 0712345678"})
    client.post("/commit", data={"data": __import__("json").dumps(p.json()["intent"])})

    # Post M-Pesa SMS payload via Make webhook
    webhook_res = client.post(
        "/api/webhook/make",
        json={"text": "Juma Ally amelipa hisa 5000 ref QX84920193", "sender": "0712345678"},
    )
    assert webhook_res.status_code == 200
    w_data = webhook_res.json()
    assert w_data["ok"]
    assert w_data["receipt"]["action"] == "contribute"
    assert w_data["receipt"]["mpesa_ref"] == "QX84920193"


def test_offline_fallback_parser(client):
    # Even with local server unreachable (or no server running), parse returns valid intent via rule fallback
    res = client.post("/parse", data={"text": "Amina amelipa hisa 5000, jamii 1000 ref QX11223344"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"]
    assert data["intent"]["action"] == "contribute"
    assert data["intent"]["member"] == "Amina"
    assert data["intent"]["mpesa_ref"] == "QX11223344"
