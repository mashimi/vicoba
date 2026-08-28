"""SQLite layer: schema, connections and atomic transactions.

Money is INTEGER everywhere (whole Tanzanian shillings) — never REAL.
Every mutation in the app runs inside a single `with transaction() as conn:`
block so balances and journal can never diverge mid-operation.
"""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id         INTEGER PRIMARY KEY,
    member_no  TEXT UNIQUE NOT NULL,          -- BSDA registration number
    name       TEXT NOT NULL,                 -- display only, duplicates allowed
    phone      TEXT,
    join_date  TEXT NOT NULL,
    exit_date  TEXT,
    status     TEXT NOT NULL DEFAULT 'active' -- active | exited
);

CREATE TABLE IF NOT EXISTS accounts (
    code TEXT PRIMARY KEY,        -- 'cash', 'hisa:3', 'loan:12', 'income:riba', ...
    name TEXT NOT NULL,
    type TEXT NOT NULL            -- asset | liability | income | expense
);CREATE TABLE IF NOT EXISTS journals (
    id              INTEGER PRIMARY KEY,
    tx_date         TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- contribute | fee | fine | loan | repay | payout | exit | expense
    description     TEXT NOT NULL,
    actor           TEXT NOT NULL,
    member_id       INTEGER REFERENCES members(id),
    mpesa_ref       TEXT,            -- M-Pesa transaction reference (e.g. QX84920193)
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS journal_lines (
    id           INTEGER PRIMARY KEY,
    journal_id   INTEGER NOT NULL REFERENCES journals(id),
    account_code TEXT NOT NULL REFERENCES accounts(code),
    debit        INTEGER NOT NULL DEFAULT 0,
    credit       INTEGER NOT NULL DEFAULT 0,
    CHECK (debit >= 0 AND credit >= 0 AND (debit = 0 OR credit = 0))
);

CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    principal   INTEGER NOT NULL,
    rate        REAL NOT NULL,              -- flat fraction, e.g. 0.10
    total_due   INTEGER NOT NULL,
    amount_paid INTEGER NOT NULL DEFAULT 0,
    issue_date  TEXT NOT NULL,
    due_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | closed
    journal_id  INTEGER REFERENCES journals(id)
);

CREATE TABLE IF NOT EXISTS loan_guarantors (
    loan_id   INTEGER NOT NULL REFERENCES loans(id),
    member_id INTEGER NOT NULL REFERENCES members(id),
    PRIMARY KEY (loan_id, member_id)
);

-- Exactly-once execution for every committed intent (money-moving or not).
CREATE TABLE IF NOT EXISTS commit_log (
    idempotency_key TEXT PRIMARY KEY,
    receipt_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Multi-user RBAC: the first account is mwenyekiti (chair → admin).
-- Rank order mirrors the committee: mwenyekiti > mhazinaji > katibu.
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'katibu',  -- mwenyekiti | mhazinaji | katibu
    pin_hash    TEXT NOT NULL,
    phone       TEXT,                            -- WhatsApp ID binding (12-digit intl)
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,       -- login | settings_change | user_created | ...
    detail      TEXT,
    ip_addr     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Server-side sessions: the cookie carries a random token; only its SHA-256
-- is stored here so a leaked database cannot be replayed as live logins.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    expires_at TEXT NOT NULL
);

-- Query performance: member statements, meeting sheets and phone lookups.
CREATE INDEX IF NOT EXISTS idx_journal_lines_journal ON journal_lines(journal_id);
CREATE INDEX IF NOT EXISTS idx_journals_member       ON journals(member_id);
CREATE INDEX IF NOT EXISTS idx_journals_tx_date      ON journals(tx_date);
CREATE INDEX IF NOT EXISTS idx_journals_kind         ON journals(kind);
CREATE INDEX IF NOT EXISTS idx_members_phone         ON members(phone);
CREATE INDEX IF NOT EXISTS idx_members_name          ON members(name);
CREATE INDEX IF NOT EXISTS idx_loans_member          ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_users_phone           ON users(phone);
CREATE INDEX IF NOT EXISTS idx_sessions_user         ON sessions(user_id);
"""

DEFAULT_SETTINGS = {
    "group_name": "",            # filled from config at init
    "interest_rate_pct": "10",   # flat interest per loan cycle
    "loan_weeks": "12",          # default repayment period
    "eligibility_multiple": "3", # max total borrowing = multiple x hisa balance
    "require_guarantors": "0",
    "mpesa_till": "",            # Group M-Pesa Till / Paybill or Phone Number
    "mpesa_name": "",            # Group M-Pesa Registered Account Name
    "local_llm_url": "http://localhost:11434/v1/chat/completions",
    "local_llm_model": "cactus",
    "llm_provider": "local",     # local | anthropic | rule
    "webhook_secret": "",        # generated at init; protects /api/webhook/make
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.db_path()), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def transaction():
    """One atomic unit of work. BEGIN IMMEDIATE takes the write lock up front
    so concurrent requests serialize instead of failing mid-commit."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)

        # Migration: ensure mpesa_ref column exists on journals table
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(journals)").fetchall()]
        if "mpesa_ref" not in cols:
            conn.execute("ALTER TABLE journals ADD COLUMN mpesa_ref TEXT")

        from . import ledger

        for code, name, type_ in [
            ("cash", "Fedha tasani (cash box)", "asset"),
            ("jamii", "Mfuko wa Jamii", "liability"),
            ("bima", "Mfuko wa Bima", "liability"),
            ("income:ada", "Mapato ya Ada", "income"),
            ("income:faini", "Mapato ya Faini", "income"),
            ("income:riba", "Mapato ya Riba", "income"),
            ("expense:matumizi", "Matumizi ya Kikundi", "expense"),
        ]:
            ledger.ensure_account(conn, code, name, type_)

        for key, value in DEFAULT_SETTINGS.items():
            if key == "group_name":
                value = value or config.group_name()
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        if not conn.execute("SELECT value FROM settings WHERE key='secret'").fetchone():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('secret', ?)", (config.new_secret(),)
            )
        # Webhook secret protects /api/webhook/make from forgery (X-VICOBA-Secret).
        if not conn.execute("SELECT value FROM settings WHERE key='webhook_secret'").fetchone():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('webhook_secret', ?)",
                (config.new_secret(),),
            )

        # Migrate the legacy single-PIN setup (pin_hash + treasurer_name settings)
        # into the first user row so the RBAC model is immediately consistent.
        # That account becomes the chairperson (mwenyekiti) — the bootstrap admin.
        if not conn.execute("SELECT 1 FROM users").fetchone():
            legacy_hash = conn.execute(
                "SELECT value FROM settings WHERE key='pin_hash'"
            ).fetchone()
            if legacy_hash:
                legacy_name = conn.execute(
                    "SELECT value FROM settings WHERE key='treasurer_name'"
                ).fetchone()
                conn.execute(
                    "INSERT INTO users(name, role, pin_hash) VALUES(?, 'mwenyekiti', ?)",
                    (legacy_name["value"] if legacy_name else "Mhazinaji", legacy_hash["value"]),
                )
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
