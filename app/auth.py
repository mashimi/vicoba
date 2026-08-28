"""Role-based access control — Mwenyekiti → Mhazinaji → Katibu.

Models the real VICOBA committee structure (kamati): the chairperson
(mwenyekiti, admin) outranks the treasurer (mhazinaji) who outranks the
secretary (katibu, read-only).

Security model (updated):
- PINs are hashed with PBKDF2-HMAC-SHA256 (100k iterations, hardware-calibrated)
  plus a per-user random salt. Legacy plain-SHA256 rows are transparently
  upgraded on the user's next successful login.
- Sessions are random opaque tokens in an HttpOnly cookie; only the SHA-256
  of the token is stored (sessions table), so a leaked database does not
  yield replayable session cookies.
- Failed logins are rate limited per client IP (in-memory, per process).
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from fastapi import HTTPException, Request

from . import db

# Rank order mirrors the committee. Higher = more power.
ROLES = {"katibu": 1, "mhazinaji": 2, "mwenyekiti": 3}

ROLE_LABEL = {
    "katibu": "Katibu (Msajili)",
    "mhazinaji": "Mhazinaji (Treasurer)",
    "mwenyekiti": "Mwenyekiti (Chair)",
}


def role_rank(role: str) -> int:
    return ROLES.get(role or "", 0)


def role_label(role: str) -> str:
    return ROLE_LABEL.get(role, role)


# ── PIN hashing (PBKDF2-HMAC-SHA256 + per-user salt) ──────────────────────

# Calibrated on the reference deployment hardware: ~0.16s per hash, which
# keeps worst-case login (verify every active committee user, typically ≤3)
# under half a second while making offline brute-force of the 4-digit PIN
# space expensive. Raise this on faster hardware if desired.
PBKDF2_ITERATIONS = 100_000
SESSION_TTL_DAYS = 30


def hash_pin(pin: str) -> str:
    """Hash a PIN with PBKDF2-HMAC-SHA256 and a fresh random salt.

    Stored format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def _is_legacy_hash(stored: str) -> bool:
    """Legacy rows stored an unsalted SHA-256 hex digest (64 hex chars)."""
    return bool(stored) and len(stored) == 64 and all(
        c in "0123456789abcdef" for c in stored.lower()
    )


def verify_pin(stored: str, pin: str) -> bool:
    """Constant-time verification supporting both legacy and PBKDF2 formats."""
    if _is_legacy_hash(stored):
        legacy = hashlib.sha256(pin.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored.lower())
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when the stored hash uses the weak legacy scheme and should be
    upgraded to PBKDF2 at the next successful login."""
    return _is_legacy_hash(stored)


def verify_login(conn: sqlite3.Connection, pin: str):
    """Check `pin` against every active user (per-user salts rule out a direct
    hash lookup). Returns (user_row, needs_upgrade) or (None, False)."""
    for row in conn.execute("SELECT * FROM users WHERE is_active=1").fetchall():
        if verify_pin(row["pin_hash"], pin):
            return row, needs_rehash(row["pin_hash"])
    return None, False


def upgrade_legacy_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> None:
    """Re-hash a legacy plain-SHA256 PIN into the PBKDF2 format."""
    conn.execute(
        "UPDATE users SET pin_hash=? WHERE id=?", (hash_pin(pin), user_id)
    )


# ── Sessions (random opaque tokens, server-side state) ────────────────────

SESSION_COOKIE = "vicoba_session"


def _token_hash(token: str) -> str:
    """Only the SHA-256 of the cookie token is persisted, so a stolen users
    table cannot be replayed as session cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, ttl_days: int = SESSION_TTL_DAYS) -> str:
    """Mint a new session token for the user and persist its hash."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at) "
        "VALUES(?, ?, datetime('now', 'localtime', ?))",
        (_token_hash(token), user_id, f"+{ttl_days} days"),
    )
    return token


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def destroy_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """Revoke every session of a user (used after a PIN change)."""
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def session_user(request: Request):
    """Return the currently authenticated user dict, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > datetime('now', 'localtime') "
            "AND u.is_active=1",
            (_token_hash(token),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def require_auth(request: Request, min_role: str = "katibu") -> dict:
    """FastAPI dependency: authenticate and enforce a minimum role.

    Usage:
        def commit_endpoint(..., user: dict = Depends(get_treasurer)): ...
    """
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Ingia PIN kwanza.")
    if role_rank(user["role"]) < role_rank(min_role):
        raise HTTPException(
            status_code=403,
            detail=f"Haki zako ni '{role_label(user['role'])}' — unahitaji '{role_label(min_role)}'.",
        )
    return user


# Reusable dependency factories — names read naturally at the route site.
def get_current_user(request: Request) -> dict:
    return require_auth(request, "katibu")


def get_treasurer(request: Request) -> dict:
    return require_auth(request, "mhazinaji")


def get_admin(request: Request) -> dict:
    return require_auth(request, "mwenyekiti")


# ── Audit log ─────────────────────────────────────────────────────────────


def audit(conn: sqlite3.Connection, user_id, action: str, detail: str = "", ip: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(user_id, action, detail, ip_addr) VALUES(?, ?, ?, ?)",
        (user_id, action, str(detail)[:500], (ip or "")[:64]),
    )


# ── Webhook secret (X-VICOBA-Secret) ──────────────────────────────────────


def webhook_secret_valid(conn: sqlite3.Connection, header_value: str) -> bool:
    """Verify the X-VICOBA-Secret header for /api/webhook/make.

    An unconfigured (empty) secret keeps the legacy open webhook working;
    production always sets one via `init_db()` so this is only meaningful
    in existing installations that never generated a secret.
    """
    expected = db.get_setting(conn, "webhook_secret", "")
    if not expected:
        return True
    return hmac.compare_digest(header_value or "", expected)


# ── WhatsApp privilege lookup ─────────────────────────────────────────────


def whatsapp_treasurer(conn: sqlite3.Connection, phone: str) -> bool:
    """Is this WhatsApp phone bound to a mhazinaji/mwenyekiti user?

    Phone binding lets the chairperson/treasurer use their normal phone number
    for privileged WhatsApp commands (expense, exit, payout, ...) without an
    extra static env list — falls back to OPENWA_TREASURER_NUMBERS in main.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return False
    suffix = digits[-9:]
    for row in conn.execute(
        "SELECT role, phone FROM users WHERE is_active=1 AND phone IS NOT NULL AND phone != ''"
    ).fetchall():
        if re.sub(r"\D", "", row["phone"])[-9:] == suffix and role_rank(row["role"]) >= 2:
            return True
    return False


# ── Login rate limiting (in-memory, per process) ──────────────────────────
# Good enough for the single-worker SQLite deployment this app targets; a
# multi-worker setup would need a shared store (e.g. Redis).

_LOGIN_WINDOW_SECONDS = 600      # 10 minute sliding window
_LOGIN_MAX_FAILURES = 5

_login_lock = threading.Lock()
_login_failures: dict = {}       # ip -> list(failure timestamps)


def login_rate_limited(ip: str) -> bool:
    """True when this IP has too many recent failed logins."""
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _login_failures[ip] = recent
        return len(recent) >= _LOGIN_MAX_FAILURES


def record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    """Reset the failure counter (called on successful login and by tests)."""
    with _login_lock:
        _login_failures.pop(ip, None)