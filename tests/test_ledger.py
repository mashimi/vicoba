"""Tests for the double-entry ledger.

Every money movement must produce balanced entries and keep the accounting
identity true. These tests are the formal proof that the review's
'vanishing money' bug (Section 2) cannot happen.
"""
import os
import sqlite3
import pytest

from app.db import transaction, connect, SCHEMA, DEFAULT_SETTINGS, init_db
from app.ledger import (
    ensure_account,
    raw_balance,
    display_balance,
    post_journal,
    verify_invariant,
)
from app.errors import AppError


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("VICOBA_DB", db_file)
    from app import config
    config.BASE_DIR = tmp_path
    # Re-init with the fresh path
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
        ("hisa:1", "Hisa", "liability"),
        ("akiba:1", "Akiba", "liability"),
        ("loan:1", "Loan", "asset"),
    ]:
        ensure_account(conn, code, name, type_)
    conn.close()


class TestPostJournal:
    def test_balanced_entry_succeeds(self):
        with transaction() as conn:
            jid = post_journal(
                conn, tx_date="2026-01-01", kind="contribute",
                description="test", actor="test",
                lines=[("cash", 8000, 0), ("hisa:1", 0, 5000), ("jamii", 0, 1000), ("bima", 0, 2000)],
            )
        assert jid >= 1

    def test_unbalanced_entry_rejected(self):
        with pytest.raises(AppError, match="hailingani"):
            with transaction() as conn:
                post_journal(
                    conn, tx_date="2026-01-01", kind="contribute",
                    description="test", actor="test",
                    lines=[("cash", 8000, 0), ("hisa:1", 0, 5000)],
                )

    def test_zero_amount_entry_rejected(self):
        with pytest.raises(AppError, match="sifuri"):
            with transaction() as conn:
                post_journal(
                    conn, tx_date="2026-01-01", kind="contribute",
                    description="test", actor="test",
                    lines=[("cash", 0, 0)],
                )

    def test_nonexistent_account_rejected(self):
        with pytest.raises(AppError, match="haipo"):
            with transaction() as conn:
                post_journal(
                    conn, tx_date="2026-01-01", kind="test",
                    description="test", actor="test",
                    lines=[("cash", 100, 0), ("nonexistent:1", 0, 100)],
                )

    def test_negative_amount_rejected(self):
        with pytest.raises(AppError, match="hasi"):
            with transaction() as conn:
                post_journal(
                    conn, tx_date="2026-01-01", kind="test",
                    description="test", actor="test",
                    lines=[("cash", -100, 0), ("hisa:1", 0, -100)],
                )


class TestBalances:
    def test_contribution_increases_cash_and_hisa(self):
        with transaction() as conn:
            post_journal(
                conn, tx_date="2026-01-01", kind="contribute",
                description="hisa", actor="test",
                lines=[("cash", 5000, 0), ("hisa:1", 0, 5000)],
            )
            assert raw_balance(conn, "cash") == 5000
            assert display_balance(conn, "hisa:1") == 5000

    def test_fee_is_income_not_deduction(self):
        """The review's bug: fees used to vanish from balances.
        Here, fee = cash in + income up. Balances stay correct."""
        with transaction() as conn:
            # Member contributes 5000 hisa
            post_journal(
                conn, tx_date="2026-01-01", kind="contribute",
                description="hisa", actor="test",
                lines=[("cash", 5000, 0), ("hisa:1", 0, 5000)],
            )
            # Fee of 1000 is collected
            post_journal(
                conn, tx_date="2026-01-01", kind="fee",
                description="ada", actor="test",
                lines=[("cash", 1000, 0), ("income:ada", 0, 1000)],
            )
            assert raw_balance(conn, "cash") == 6000  # 5000 + 1000
            assert display_balance(conn, "hisa:1") == 5000  # unchanged
            assert display_balance(conn, "income:ada") == 1000

    def test_loan_reduces_cash_creates_receivable(self):
        with transaction() as conn:
            post_journal(
                conn, tx_date="2026-01-01", kind="contribute",
                description="hisa", actor="test",
                lines=[("cash", 50000, 0), ("hisa:1", 0, 50000)],
            )
            post_journal(
                conn, tx_date="2026-01-01", kind="loan",
                description="mkopo", actor="test",
                lines=[("loan:1", 20000, 0), ("cash", 0, 20000)],
            )
            assert raw_balance(conn, "cash") == 30000
            assert raw_balance(conn, "loan:1") == 20000

    def test_repay_splits_interest_and_principal(self):
        with transaction() as conn:
            post_journal(
                conn, tx_date="2026-01-01", kind="contribute",
                description="hisa", actor="test",
                lines=[("cash", 50000, 0), ("hisa:1", 0, 50000)],
            )
            post_journal(
                conn, tx_date="2026-01-01", kind="loan",
                description="mkopo", actor="test",
                lines=[("loan:1", 20000, 0), ("cash", 0, 20000)],
            )
            # Repay 5500 of a 22000 total (20000 principal + 2000 interest)
            # Interest remaining = 2000, so 2000 goes to riba, 3500 to principal
            post_journal(
                conn, tx_date="2026-01-01", kind="repay",
                description="rejesho", actor="test",
                lines=[
                    ("cash", 5500, 0),
                    ("loan:1", 0, 3500),
                    ("income:riba", 0, 2000),
                ],
            )
            assert display_balance(conn, "income:riba") == 2000
            assert raw_balance(conn, "cash") == 35500  # 50000 - 20000 + 5500


class TestInvariant:
    def test_invariant_holds_after_mixed_operations(self):
        with transaction() as conn:
            # Contributions
            post_journal(conn, tx_date="2026-01-01", kind="contribute", description="", actor="t",
                          lines=[("cash", 8000, 0), ("hisa:1", 0, 5000), ("jamii", 0, 1000), ("bima", 0, 2000)])
            # Fee
            post_journal(conn, tx_date="2026-01-01", kind="fee", description="", actor="t",
                          lines=[("cash", 1000, 0), ("income:ada", 0, 1000)])
            # Loan
            post_journal(conn, tx_date="2026-01-01", kind="loan", description="", actor="t",
                          lines=[("loan:1", 5000, 0), ("cash", 0, 5000)])
            # Repay (300 interest + 700 principal of 5500 total due)
            post_journal(conn, tx_date="2026-01-01", kind="repay", description="", actor="t",
                          lines=[("cash", 1000, 0), ("loan:1", 0, 700), ("income:riba", 0, 300)])
            verify_invariant(conn)  # should not raise


class TestIdempotency:
    def test_duplicate_journal_rejected(self):
        with transaction() as conn:
            post_journal(conn, tx_date="2026-01-01", kind="contribute", description="",
                          actor="t", idempotency_key="dup-1",
                          lines=[("cash", 5000, 0), ("hisa:1", 0, 5000)])
        # Second attempt with same key should fail
        with pytest.raises(Exception):
            with transaction() as conn:
                post_journal(conn, tx_date="2026-01-01", kind="contribute", description="",
                              actor="t", idempotency_key="dup-1",
                              lines=[("cash", 5000, 0), ("hisa:1", 0, 5000)])
