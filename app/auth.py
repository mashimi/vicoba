"""Role-based access control — Mwenyekiti → Mhazinaji → Katibu.

Models the real VICOBA committee structure (kamati): the chairperson
(mwenyekiti, admin) outranks the treasurer (mhazinaji) who outranks the
secretary (katibu, read-only). Replaces the old single-treasurer-PIN model.

Identity is a PIN (4-digit, hashed with SHA-256 exactly like the legacy app);
the sha256 digest is stored in the session cookie and used as the lookup key
so every endpoint stays stateless.
"""
import hashlib
import re
import sqlite3

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


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def find_active_user(conn: sqlite3.Connection, pin_hash: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM users WHERE pin_hash=? AND is_active=1", (pin_hash,)
    ).fetchone()


def session_user(request: Request):
    """Return the currently authenticated user dict, or None."""
    pin = request.cookies.get("vicoba_pin")
    if not pin:
        return None
    conn = db.connect()
    try:
        row = find_active_user(conn, pin)
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
    import hmac

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