"""Integration tests for the commit engine.

These exercises run full parse→commit flows and verify:
- Fees are income (Model A from the review), never balance deductions
- Loans check cash availability and eligibility
- Repayments split principal vs interest
- Idempotency prevents double-charging
- The invariant holds after every operation
"""
import json
import pytest

from app.db import transaction, connect, SCHEMA
from app.ledger import ensure_account, verify_invariant, raw_balance, display_balance
from app.members import register
from app.parser import parse as rule_parse
from app.commits import (
    commit_register, commit_contribute, commit_fee, commit_fine,
    commit_loan, commit_repay, commit_expense, commit_exit,
)
from app.errors import AppError, DuplicateCommit
from app import config


def _commit_or_dupe(fn, *args, **kwargs):
    """Call a commit function; if DuplicateCommit is raised, return the stored receipt."""
    try:
        return fn(*args, **kwargs)
    except DuplicateCommit as e:
        return e.receipt


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    monkeypatch.setenv("GROUP_NAME", "Test")
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
    # Default settings
    for k, v in [
        ("interest_rate_pct", "10"), ("loan_weeks", "12"),
        ("eligibility_multiple", "3"), ("require_guarantors", "0"),
    ]:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT DO NOTHING", (k, v))
    conn.close()


def _reg(conn, name, **kw):
    return register(conn, name, **kw)


class TestContributionFlow:
    def test_full_contribution(self):
        """Juma pays Hisa 5,000 + Jamii 1,000 + Bima 2,000.
        Cash box should be 8,000. Hisa=5000. Jamii=1000. Bima=2000."""
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            r = commit_contribute(
                conn, member_name="Juma Ally",
                amounts={"hisa": 5000, "jamii": 1000, "bima": 2000},
                actor="test", idem_key="t1",
            )
            assert r["ok"]
            assert raw_balance(conn, "cash") == 8000
            assert display_balance(conn, "hisa:1") == 5000
            assert display_balance(conn, "jamii") == 1000
            assert display_balance(conn, "bima") == 2000
            verify_invariant(conn)

    def test_fee_does_not_vanish_money(self):
        """The review's core bug: in the old code, fees were deducted from savings.
        Here, fees are cash-in + income-up. Cash goes UP."""
        with transaction() as conn:
            _reg(conn, "Juma")
            commit_contribute(
                conn, member_name="Juma", amounts={"hisa": 5000},
                actor="t", idem_key="a",
            )
            commit_fee(conn, amount=1000, actor="t", idem_key="b")
            assert raw_balance(conn, "cash") == 6000  # 5000 + 1000
            assert display_balance(conn, "hisa:1") == 5000  # unchanged!
            assert display_balance(conn, "income:ada") == 1000
            verify_invariant(conn)

    def test_fine_does_not_vanish_money(self):
        with transaction() as conn:
            _reg(conn, "Juma")
            commit_contribute(
                conn, member_name="Juma", amounts={"hisa": 5000},
                actor="t", idem_key="a",
            )
            commit_fine(conn, member_name="Juma", amount=500, actor="t", idem_key="c")
            assert raw_balance(conn, "cash") == 5500  # 5000 + 500
            assert display_balance(conn, "hisa:1") == 5000  # unchanged
            assert display_balance(conn, "income:faini") == 500
            verify_invariant(conn)


class TestLoanFlow:
    def _setup_juma_with_hisa(self, conn):
        _reg(conn, "Juma Ally")
        commit_contribute(
            conn, member_name="Juma Ally", amounts={"hisa": 20000},
            actor="t", idem_key="setup",
        )

    def test_basic_loan(self):
        with transaction() as conn:
            self._setup_juma_with_hisa(conn)
            r = commit_loan(
                conn, member_name="Juma Ally", amount=10000,
                guarantors=None, actor="t", idem_key="loan1",
            )
            assert r["ok"]
            assert r["total_due"] == 11000  # 10% flat
            assert raw_balance(conn, "cash") == 10000  # 20000 - 10000
            verify_invariant(conn)

    def test_loan_exceeds_cash_available(self):
        with transaction() as conn:
            self._setup_juma_with_hisa(conn)
            with pytest.raises(AppError, match="haitoshi"):
                commit_loan(
                    conn, member_name="Juma Ally", amount=50000,
                    guarantors=None, actor="t", idem_key="loan2",
                )

    def test_loan_exceeds_eligibility(self):
        with transaction() as conn:
            self._setup_juma_with_hisa(conn)
            # 20000 hisa x 3 = 60000 max, but only 20000 cash, so cash is the limit
            # Let's give more cash
            commit_fee(conn, amount=50000, actor="t", idem_key="fee-big")
            # Now cash = 70000, eligibility = 60000
            with pytest.raises(AppError, match="upitaji mdogo"):
                commit_loan(
                    conn, member_name="Juma Ally", amount=65000,
                    guarantors=None, actor="t", idem_key="loan3",
                )

    def test_repay_splits_interest_principal(self):
        with transaction() as conn:
            self._setup_juma_with_hisa(conn)
            commit_loan(
                conn, member_name="Juma Ally", amount=10000,
                guarantors=None, actor="t", idem_key="l1",
            )
            r = commit_repay(
                conn, member_name="Juma Ally", amount=5500,
                actor="t", idem_key="r1",
            )
            assert r["interest_portion"] == 1000  # 1000 interest, all consumed
            assert r["principal_portion"] == 4500
            assert display_balance(conn, "income:riba") == 1000
            assert r["remaining"] == 5500
            verify_invariant(conn)

    def test_full_repay_closes_loan(self):
        with transaction() as conn:
            self._setup_juma_with_hisa(conn)
            commit_loan(
                conn, member_name="Juma Ally", amount=10000,
                guarantors=None, actor="t", idem_key="l1",
            )
            # Pay 11000 total
            commit_repay(conn, member_name="Juma Ally", amount=6000, actor="t", idem_key="r1")
            r = commit_repay(conn, member_name="Juma Ally", amount=5000, actor="t", idem_key="r2")
            assert "kamilika" in r["message"]
            verify_invariant(conn)


class TestIdempotency:
    def test_double_contribute_returns_same_receipt(self):
        with transaction() as conn:
            _reg(conn, "Juma")
            r1 = _commit_or_dupe(
                commit_contribute,
                conn, member_name="Juma", amounts={"hisa": 5000},
                actor="t", idem_key="idem-1",
            )
        with transaction() as conn:
            r2 = _commit_or_dupe(
                commit_contribute,
                conn, member_name="Juma", amounts={"hisa": 5000},
                actor="t", idem_key="idem-1",
            )
        # Second call returns the original receipt via DuplicateCommit
        assert r1["journal_id"] == r2["journal_id"]
        # Balances should be 5000, not 10000
        with transaction() as conn:
            assert display_balance(conn, "hisa:1") == 5000


class TestParser:
    def test_contribute_parse(self):
        intent = rule_parse("Amina amelipa hisa 5000, jamii 1000, bima 2000")
        assert intent.action == "contribute"
        assert intent.member == "Amina"
        assert intent.amounts == {"hisa": 5000, "jamii": 1000, "bima": 2000}

    def test_register_parse(self):
        intent = rule_parse("msajili mwanachama kwa jina Juma Ally 0712345678")
        assert intent.action == "register"
        assert intent.member == "Juma Ally"

    def test_loan_parse(self):
        intent = rule_parse("Juma kopa 50000")
        assert intent.action == "loan"
        assert intent.member == "Juma"
        assert intent.amount == 50000

    def test_repay_parse(self):
        intent = rule_parse("Juma amelipa mkopo 10000")
        assert intent.action == "repay"
        assert intent.member == "Juma"
        assert intent.amount == 10000

    def test_fine_parse(self):
        intent = rule_parse("faini ya Amina 2000")
        assert intent.action == "fine"
        assert intent.member == "Amina"
        assert intent.amount == 2000

    def test_statement_query(self):
        intent = rule_parse("taarifa ya Juma")
        assert intent.action == "member_statement"
        assert intent.member == "Juma"

    def test_group_position_query(self):
        intent = rule_parse("maendeleo ya kikundi")
        assert intent.action == "group_position"

    def test_unknown_falls_through(self):
        intent = rule_parse("habari za mtaa")
        assert intent.action == "unknown"

    def test_exit_parse(self):
        intent = rule_parse("ondoa mwanachama Juma Ally")
        assert intent.action == "exit"
        assert intent.member == "Juma Ally"


class TestExitFlow:
    def test_exit_with_refundable_hisa_and_akiba(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            commit_contribute(
                conn, member_name="Juma Ally",
                amounts={"hisa": 10000, "akiba": 5000, "jamii": 1000},
                actor="test", idem_key="exit_setup",
            )
            # Cash box has 16000 total (10000 + 5000 + 1000)
            r = commit_exit(conn, member_name="Juma Ally", actor="test", idem_key="exit_key_1")
            assert r["ok"]
            assert r["refund_hisa"] == 10000
            assert r["refund_akiba"] == 5000
            assert r["total_refund"] == 15000
            # Cash box should now have 1000 TSH (16000 - 15000) because jamii (1000) is communal
            assert raw_balance(conn, "cash") == 1000
            verify_invariant(conn)

    def test_exit_blocked_by_active_loan(self):
        with transaction() as conn:
            _reg(conn, "Juma Ally")
            commit_contribute(conn, member_name="Juma Ally", amounts={"hisa": 20000}, actor="t", idem_key="s1")
            commit_loan(conn, member_name="Juma Ally", amount=10000, guarantors=None, actor="t", idem_key="l1")
            with pytest.raises(AppError, match="mkopo hai"):
                commit_exit(conn, member_name="Juma Ally", actor="t", idem_key="exit_fail")
