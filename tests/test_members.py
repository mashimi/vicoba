"""Tests for member registration, resolution, and the Two-Asha fix.
"""
import pytest

from app.db import transaction, connect, SCHEMA
from app.ledger import ensure_account
from app.members import register, resolve, must_be_active, member_balance_summary, exit_settlement
from app.errors import AppError
from app import config


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    monkeypatch.setenv("GROUP_NAME", "Test Group")
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


def _reg(conn, name, **kw):
    return register(conn, name, **kw)


class TestRegistration:
    def test_basic_registration(self):
        with transaction() as conn:
            m = _reg(conn, "Juma Ally")
            assert m["name"] == "Juma Ally"
            assert m["member_no"] == "BSDA-001"
            assert m["status"] == "active"

    def test_auto_increments_member_no(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            m = _reg(conn, "Amina Mohamed")
            assert m["member_no"] == "BSDA-002"

    def test_custom_member_no(self):
        with transaction() as conn:
            m = _reg(conn, "Fatuma", member_no="BSDA-010")
            assert m["member_no"] == "BSDA-010"

    def test_duplicate_member_no_rejected(self):
        with pytest.raises(AppError, match="inatumika"):
            with transaction() as conn:
                _reg(conn, "Juma", member_no="BSDA-010")
                _reg(conn, "Amina", member_no="BSDA-010")

    def test_phone_stored(self):
        with transaction() as conn:
            m = _reg(conn, "Juma", phone="0712345678")
            assert m["phone"] == "0712345678"

    def test_invalid_phone_rejected(self):
        with pytest.raises(AppError, match="simu"):
            with transaction() as conn:
                _reg(conn, "Juma", phone="123")


class TestTwoAsha:
    """The review: 'Every VICOBA group has two Ashas. The second registration fails.'
    With member numbers, both Ashas can coexist."""

    def test_duplicate_names_allowed(self):
        with transaction() as conn:
            a1 = _reg(conn, "Asha Mohamed")
            a2 = _reg(conn, "Asha Mohamed")  # same name, new member
            assert a1["member_no"] != a2["member_no"]
            assert a1["name"] == a2["name"] == "Asha Mohamed"

    def test_exact_name_resolve_ambiguous(self):
        with transaction() as conn:
            _reg(conn, "Asha Mohamed")
            _reg(conn, "Asha Mohamed")
            with pytest.raises(AppError, match="wanachama wengi"):
                resolve(conn, "Asha Mohamed")

    def test_member_no_resolve_unambiguous(self):
        with transaction() as conn:
            _reg(conn, "Asha Mohamed")
            a2 = _reg(conn, "Asha Mohamed")
            result = resolve(conn, a2["member_no"])
            assert result["id"] == a2["id"]


class TestNameNormalization:
    """'juma', 'Juma ', 'JUMA' should all match."""

    def test_case_insensitive(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            m = resolve(conn, "JUMA ALLY")
            assert m is not None

    def test_whitespace_trimmed(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            m = resolve(conn, "  Juma Ally  ")
            assert m is not None

    def test_typo_in_name_not_found(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            with pytest.raises(AppError, match="Hakuna mwanachama"):
                resolve(conn, "Jumaa Ally")


class TestExitSettlement:
    def test_hisa_and_akiba_refundable_jamii_bima_not(self):
        with transaction() as conn:
            m = _reg(conn, "Juma")
            s = exit_settlement(conn, m)
            assert s["payable"] == 0  # no balances yet
            assert "Jamii na Bima" in s["note"]

    def test_cannot_exit_with_active_loan(self):
        with transaction() as conn:
            m = _reg(conn, "Juma")
            # Simulate an active loan
            conn.execute(
                "INSERT INTO loans(member_id, principal, rate, total_due, amount_paid, issue_date, due_date) "
                "VALUES(?, 10000, 0.1, 11000, 0, '2026-01-01', '2026-03-26')",
                (m["id"],),
            )
            s = exit_settlement(conn, m)
            assert not s["can_exit"]
            assert "mkopo hai" in s["note"]
