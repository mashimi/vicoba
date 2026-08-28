"""Member registry: BSDA member numbers are the identity, names are display-only.

The "Two-Asha problem" from the review is solved structurally: names may
duplicate freely; any ambiguous name reference must be resolved to a member
number before money moves.
"""
import re
import sqlite3
from typing import Optional

from . import config, ledger
from .errors import AppError


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip())


def register(
    conn: sqlite3.Connection, name: str, phone: Optional[str] = None, member_no: Optional[str] = None
) -> sqlite3.Row:
    name = normalize_name(name)
    if not re.fullmatch(r"[A-Za-z\u00C0-\u024F' -]{2,60}", name):
        raise AppError(f"Jina '{name}' halipo sahihi. Tumia herufi tu.")
    if phone:
        phone = re.sub(r"\D", "", phone)
        if not re.fullmatch(r"\d{9,12}", phone):
            raise AppError("Namba ya simu si sahihi (mfano: 0712345678)")

    if member_no:
        member_no = member_no.strip().upper()
        if conn.execute("SELECT 1 FROM members WHERE member_no=?", (member_no,)).fetchone():
            raise AppError(f"Namba {member_no} inatumika already. Chagua nyingine.")
    else:
        row = conn.execute(
            "SELECT member_no FROM members ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            try:
                last = int(row["member_no"].split("-")[-1])
            except (ValueError, IndexError):
                # Fallback: use total count if last number is non-standard
                last = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        else:
            last = 0
        member_no = f"BSDA-{last + 1:03d}"

    cur = conn.execute(
        "INSERT INTO members(member_no, name, phone, join_date) VALUES(?, ?, ?, ?)",
        (member_no, name, phone, config.today()),
    )
    mid = cur.lastrowid
    # Every member gets their own hisa and akiba (savings) sub-accounts.
    ledger.ensure_account(conn, f"hisa:{mid}", f"Hisa ya {name}", "liability")
    ledger.ensure_account(conn, f"akiba:{mid}", f"Akiba ya {name}", "liability")
    return conn.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()


def resolve(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Resolve a member reference: exact member number, exact full name, or a
    name appearing inside free text. Raises with actionable Swahili messages."""
    ref = normalize_name(ref)
    if not ref:
        raise AppError("Jina la mwanachama limekosekana.")
    row = conn.execute(
        "SELECT * FROM members WHERE member_no = ? COLLATE NOCASE", (ref,)
    ).fetchone()
    if row:
        return row
    rows = conn.execute(
        "SELECT * FROM members WHERE name = ? COLLATE NOCASE", (ref,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        nos = ", ".join(f"{r['name']} ({r['member_no']})" for r in rows)
        raise AppError(f"Kuna wanachama wengi kwa jina hilo: {nos}. Tumia namba ya mwanachama.")

    # Fallback: find a registered member's name inside the reference text
    # (longest names win, so "Juma Ally" beats a bare "Juma").
    all_members = conn.execute(
        "SELECT * FROM members ORDER BY LENGTH(name) DESC"
    ).fetchall()
    words = set(re.findall(r"[A-Za-z\u00C0-\u024F']+", ref.lower()))
    matches = []
    for m in all_members:
        name_words = [w for w in re.findall(r"[A-Za-z\u00C0-\u024F']+", m["name"].lower()) if w]
        if name_words and all(w in words for w in name_words):
            matches.append(m)
    unique = {m["id"]: m for m in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        nos = ", ".join(f"{m['name']} ({m['member_no']})" for m in unique.values())
        raise AppError(f"Majina mengi yamefanana: {nos}. Tumia namba ya mwanachama.")
    raise AppError(f"Hakuna mwanachama kwa jina '{ref}'. Msajili kwanza.")


def must_be_active(member: sqlite3.Row) -> None:
    if member["status"] != "active":
        raise AppError(
            f"{member['name']} ({member['member_no']}) ameisha kuwa mwanachama (ametoka)."
        )


def member_balance_summary(conn: sqlite3.Connection, member: sqlite3.Row) -> dict:
    mid = member["id"]
    loans_out = conn.execute(
        "SELECT COALESCE(SUM(total_due - amount_paid), 0) AS d FROM loans "
        "WHERE member_id=? AND status='active'",
        (mid,),
    ).fetchone()["d"]
    return {
        "member_id": mid,
        "member_no": member["member_no"],
        "name": member["name"],
        "phone": member["phone"],
        "status": member["status"],
        "join_date": member["join_date"],
        "hisa": ledger.display_balance(conn, f"hisa:{mid}"),
        "akiba": ledger.display_balance(conn, f"akiba:{mid}"),
        "deni_lichangiwa": int(loans_out),  # total_due remaining incl. interest
        "equity": ledger.display_balance(conn, f"hisa:{mid}")
        + ledger.display_balance(conn, f"akiba:{mid}"),
    }


def member_statement(conn: sqlite3.Connection, member: sqlite3.Row) -> dict:
    summary = member_balance_summary(conn, member)
    summary["transactions"] = [
        dict(r) for r in conn.execute(
            "SELECT id, tx_date, kind, description FROM journals "
            "WHERE member_id=? ORDER BY id DESC LIMIT 100",
            (member["id"],),
        ).fetchall()
    ]
    summary["loans"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM loans WHERE member_id=? ORDER BY id DESC", (member["id"],)
        ).fetchall()
    ]
    return summary


def exit_settlement(conn: sqlite3.Connection, member: sqlite3.Row) -> dict:
    """What the member is owed on exit. Per VICOBA rules hisa + akiba are
    refundable; jamii and bima are communal funds and are NOT refunded —
    this is exactly what the old schema could not compute."""
    s = member_balance_summary(conn, member)
    active_loans = conn.execute(
        "SELECT COUNT(*) AS n FROM loans WHERE member_id=? AND status='active'",
        (member["id"],),
    ).fetchone()["n"]
    return {
        **s,
        "can_exit": active_loans == 0,
        "active_loans": active_loans,
        "payable": s["hisa"] + s["akiba"] if active_loans == 0 else 0,
        "note": "Hisa + Akiba zinarejeshwa. Jamii na Bima ni za kikundi — hazirejeshwi."
        if active_loans == 0
        else "Ana mkopo hai — alipe kwanza kabla ya kutoka.",
    }
