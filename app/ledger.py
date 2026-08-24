"""Double-entry ledger engine.

Every money movement is a balanced journal entry (sum of debits == sum of
credits), so the accounting identity holds at every instant:

    cash + outstanding_loans = hisa + akiba + jamii + bima + net_income

Nothing may ever update a balance directly: balances are always derived by
summing journal_lines. The review's "vanishing money" bug (fees deducted from
a savings balance without any cash movement) is structurally impossible here.
"""
import sqlite3
from typing import Iterable, Optional

from .errors import AppError

CREDIT_NORMAL = ("liability", "income")  # displayed/report as credit balance


def ensure_account(conn: sqlite3.Connection, code: str, name: str, type_: str) -> None:
    conn.execute(
        "INSERT INTO accounts(code, name, type) VALUES(?, ?, ?) "
        "ON CONFLICT(code) DO NOTHING",
        (code, name, type_),
    )


def account_exists(conn: sqlite3.Connection, code: str) -> bool:
    return conn.execute("SELECT 1 FROM accounts WHERE code=?", (code,)).fetchone() is not None


def raw_balance(conn: sqlite3.Connection, code: str) -> int:
    """Signed balance: debits minus credits."""
    row = conn.execute(
        "SELECT COALESCE(SUM(debit - credit), 0) AS b FROM journal_lines WHERE account_code=?",
        (code,),
    ).fetchone()
    return int(row["b"])


def display_balance(conn: sqlite3.Connection, code: str) -> int:
    """Positive number for the account's natural side (asset/income-expense aware)."""
    row = conn.execute("SELECT type FROM accounts WHERE code=?", (code,)).fetchone()
    bal = raw_balance(conn, code)
    if row and row["type"] in CREDIT_NORMAL:
        return -bal
    return bal


def balances_by_prefix(conn: sqlite3.Connection, prefix: str) -> dict:
    rows = conn.execute(
        "SELECT code FROM accounts WHERE code LIKE ? ORDER BY code", (prefix + "%",)
    ).fetchall()
    return {r["code"]: raw_balance(conn, r["code"]) for r in rows}


def post_journal(
    conn: sqlite3.Connection,
    *,
    tx_date: str,
    kind: str,
    description: str,
    actor: str,
    lines: Iterable,
    member_id: Optional[int] = None,
    mpesa_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """lines: iterable of (account_code, debit, credit). Must balance and every
    account must exist — otherwise the whole transaction raises (caller rolls back)."""
    lines = [(c, int(d), int(cr)) for c, d, cr in lines]
    if not lines:
        raise AppError("Hakuna mstari wa kuweka (amount is zero?)")
    total_d, total_c = 0, 0
    for code, d, c in lines:
        if d < 0 or c < 0:
            raise AppError("Kiasi hakiwezi kuwa hasi")
        if (d == 0) != (c == 0):  # exactly one side per line
            total_d += d
            total_c += c
        elif d == 0 and c == 0:
            raise AppError("Mstari wa sifuri")
        else:
            raise AppError("Mstari una debit na credit kwa pamoja")
        if not account_exists(conn, code):
            raise AppError(f"Akaunti '{code}' haipo")
    if total_d != total_c or total_d == 0:
        raise AppError("Mingatio ya fedha hailingani (debits != credits)")

    cur = conn.execute(
        "INSERT INTO journals(tx_date, kind, description, actor, member_id, mpesa_ref, idempotency_key) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (tx_date, kind, description, actor, member_id, mpesa_ref, idempotency_key),
    )
    jid = cur.lastrowid
    conn.executemany(
        "INSERT INTO journal_lines(journal_id, account_code, debit, credit) VALUES(?, ?, ?, ?)",
        [(jid, code, d, c) for code, d, c in lines],
    )
    return jid


def journal_receipt(conn: sqlite3.Connection, journal_id: int) -> dict:
    """Human-readable receipt of a posted journal entry."""
    j = conn.execute("SELECT * FROM journals WHERE id=?", (journal_id,)).fetchone()
    lines = conn.execute(
        "SELECT l.*, a.name AS account_name, a.type AS account_type "
        "FROM journal_lines l JOIN accounts a ON a.code = l.account_code "
        "WHERE l.journal_id=? ORDER BY l.id",
        (journal_id,),
    ).fetchall()
    return {
        "journal_id": journal_id,
        "date": j["tx_date"],
        "kind": j["kind"],
        "description": j["description"],
        "actor": j["actor"],
        "mpesa_ref": j["mpesa_ref"],
        "lines": [
            {
                "account": r["account_code"],
                "account_name": r["account_name"],
                "debit": r["debit"],
                "credit": r["credit"],
            }
            for r in lines
        ],
    }


def verify_invariant(conn: sqlite3.Connection) -> None:
    """cash + loans = hisa + akiba + funds + income - expenses.
    Used by the health endpoint and the test suite; raises if books don't balance."""
    cash = raw_balance(conn, "cash")
    loans = sum(b for b in balances_by_prefix(conn, "loan:").values())
    hisa = -sum(raw_balance(conn, c) for c in balances_by_prefix(conn, "hisa:"))
    akiba = -sum(raw_balance(conn, c) for c in balances_by_prefix(conn, "akiba:"))
    jamii = -raw_balance(conn, "jamii")
    bima = -raw_balance(conn, "bima")
    income = -sum(raw_balance(conn, c) for c in ("income:ada", "income:faini", "income:riba"))
    expenses = raw_balance(conn, "expense:matumizi")
    if cash + loans != hisa + akiba + jamii + bima + income - expenses:
        raise AppError(
            f"Mingatio ya mahesabu hailingani: cash={cash} loans={loans} vs "
            f"hisa={hisa} akiba={akiba} jamii={jamii} bima={bima} income={income} expense={expenses}"
        )
