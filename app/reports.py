"""Read/report tools — the queries the review said were missing.

The old app was "a treasurer that can write but not read." Every function
here is a pure SELECT; no mutations, no LLM needed.
"""
import sqlite3
from typing import Optional

from . import ledger, members


def member_statement(conn: sqlite3.Connection, member_name: str) -> dict:
    member = members.resolve(conn, member_name)
    return members.member_statement(conn, member)


def who_hasnt_paid_today(conn: sqlite3.Connection, required: dict) -> dict:
    """Who hasn't contributed today. `required` is e.g. {hisa: 5000}.
    Returns a list of members who have no contribution journal entry today."""
    from .config import today

    today_str = today()
    active = conn.execute(
        "SELECT id, name, member_no FROM members WHERE status='active' ORDER BY name"
    ).fetchall()

    paid_ids = set(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT member_id FROM journals WHERE tx_date=? AND kind='contribute'",
            (today_str,),
        ).fetchall()
    )

    missing = []
    for m in active:
        if m["id"] not in paid_ids:
            missing.append({"id": m["id"], "name": m["name"], "member_no": m["member_no"]})
    return {
        "date": today_str,
        "required": required,
        "total_active": len(active),
        "paid_today": len(paid_ids),
        "missing": missing,
    }


def group_position(conn: sqlite3.Connection) -> dict:
    """Full group snapshot — the single most useful screen at a kutaniko."""
    cash = ledger.raw_balance(conn, "cash")
    jamii = ledger.display_balance(conn, "jamii")
    bima = ledger.display_balance(conn, "bima")

    member_rows = conn.execute(
        "SELECT id, name, member_no, status FROM members ORDER BY name"
    ).fetchall()
    member_summaries = []
    total_hisa = 0
    total_akiba = 0
    total_outstanding = 0

    for m in member_rows:
        s = members.member_balance_summary(conn, m)
        member_summaries.append(s)
        total_hisa += s["hisa"]
        total_akiba += s["akiba"]
        total_outstanding += s["deni_lichangiwa"]

    income_ada = ledger.display_balance(conn, "income:ada")
    income_faini = ledger.display_balance(conn, "income:faini")
    income_riba = ledger.display_balance(conn, "income:riba")
    total_income = income_ada + income_faini + income_riba
    expenses = ledger.display_balance(conn, "expense:matumizi")

    active_count = sum(1 for m in member_rows if m["status"] == "active")
    loan_rows = conn.execute(
        "SELECT * FROM loans WHERE status='active'"
    ).fetchall()

    return {
        "cash": cash,
        "jamii": jamii,
        "bima": bima,
        "total_hisa": total_hisa,
        "total_akiba": total_akiba,
        "total_outstanding_loans": total_outstanding,
        "total_income": total_income,
        "income_breakdown": {"ada": income_ada, "faini": income_faini, "riba": income_riba},
        "total_expenses": expenses,
        "net_income": total_income - expenses,
        "active_members": active_count,
        "total_members": len(member_rows),
        "active_loans": len(loan_rows),
        "members": member_summaries,
    }


def meeting_sheet(conn: sqlite3.Connection) -> dict:
    """Printable weekly meeting summary (ripoti ya kutaniko).
    The review called this 'the feature that saves the secretary an hour every week.'"""
    from .config import today

    today_str = today()
    journals = conn.execute(
        "SELECT j.*, m.name AS member_name, m.member_no "
        "FROM journals j LEFT JOIN members m ON m.id = j.member_id "
        "WHERE j.tx_date=? ORDER BY j.id",
        (today_str,),
    ).fetchall()

    day_total_in = 0
    day_total_out = 0
    entries = []
    for j in journals:
        lines = conn.execute(
            "SELECT * FROM journal_lines WHERE journal_id=?", (j["id"],)
        ).fetchall()
        dr = sum(l["debit"] for l in lines)
        cr = sum(l["credit"] for l in lines)
        if j["kind"] in ("contribute", "repay", "fee", "fine"):
            day_total_in += dr
        elif j["kind"] in ("loan", "payout", "expense"):
            day_total_out += cr
        entries.append({
            "journal_id": j["id"],
            "kind": j["kind"],
            "description": j["description"],
            "member": j["member_name"],
            "member_no": j["member_no"],
            "debit": dr,
            "credit": cr,
        })

    return {
        "date": today_str,
        "entries": entries,
        "day_cash_in": day_total_in,
        "day_cash_out": day_total_out,
        "day_net": day_total_in - day_total_out,
        "cash_balance": ledger.raw_balance(conn, "cash"),
    }


def gawio_estimate(conn: sqlite3.Connection) -> dict:
    """Rough cycle-end profit distribution estimate.

    In a real VICOBA, gawio distributes net income proportionally to hisa.
    This gives the treasurer a preview before the annual close."""
    pos = group_position(conn)
    net = pos["net_income"]
    total_hisa = pos["total_hisa"]
    if total_hisa == 0:
        return {
            "distributable": net,
            "total_hisa": 0,
            "per_hisa_rate": 0,
            "members": [],
            "note": "Hakuna hisa — gawio haiwezekani.",
        }
    rate = net / total_hisa
    dist = []
    for m in pos["members"]:
        if m["status"] != "active" or m["hisa"] <= 0:
            continue
        share = int(m["hisa"] * rate)
        dist.append({"name": m["name"], "member_no": m["member_no"], "hisa": m["hisa"], "gawio": share})
    return {
        "distributable": net,
        "total_hisa": total_hisa,
        "per_hisa_rate": round(rate, 4),
        "members": dist,
    }
