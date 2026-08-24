"""Deterministic commit engine — every mutation the app supports.

All money movement goes through ledger.post_journal() inside a single
transaction() block. The LLM is never in the execution path.

Each public function returns a receipt dict suitable for the frontend.
"""
import sqlite3
from datetime import date, timedelta
from typing import Optional

from . import config, db, ledger, members
from .errors import AppError, DuplicateCommit


def _check_idempotency(conn: sqlite3.Connection, idem_key: str) -> None:
    """If this exact intent was already committed, return the original receipt."""
    row = conn.execute(
        "SELECT receipt_json FROM commit_log WHERE idempotency_key=?", (idem_key,)
    ).fetchone()
    if row:
        raise DuplicateCommit(__import__("json").loads(row["receipt_json"]))


def _log_commit(conn: sqlite3.Connection, idem_key: str, receipt: dict) -> None:
    conn.execute(
        "INSERT INTO commit_log(idempotency_key, receipt_json) VALUES(?, ?)",
        (idem_key, __import__("json").dumps(receipt, ensure_ascii=False)),
    )


def _setting_int(conn: sqlite3.Connection, key: str) -> int:
    return int(db.get_setting(conn, key, "0"))


# ── Registration ────────────────────────────────────────────────────────

def commit_register(
    conn: sqlite3.Connection, *, name: str, phone: Optional[str] = None,
    actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.register(conn, name, phone=phone)
    receipt = {
        "ok": True,
        "action": "register",
        "message": f"✅ {member['name']} ({member['member_no']}) amesajiliwa.",
        "member": {"id": member["id"], "member_no": member["member_no"], "name": member["name"]},
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Contribution (hisa / jamii / bima — the review's Model A fix) ───────

def commit_contribute(
    conn: sqlite3.Connection, *, member_name: str, amounts: dict,
    actor: str, idem_key: str, mpesa_ref: Optional[str] = None,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    members.must_be_active(member)
    mid = member["id"]
    today_str = config.today()

    lines = []
    desc_parts = []

    for cat, amt in amounts.items():
        amt = int(amt)
        if amt <= 0:
            continue
        if cat == "hisa":
            lines.append((f"cash", amt, 0))           # cash in
            lines.append((f"hisa:{mid}", 0, amt))     # member equity up
            desc_parts.append(f"Hisa {amt:,}")
        elif cat == "jamii":
            lines.append(("cash", amt, 0))
            lines.append(("jamii", 0, amt))           # communal fund up
            desc_parts.append(f"Jamii {amt:,}")
        elif cat == "bima":
            lines.append(("cash", amt, 0))
            lines.append(("bima", 0, amt))            # insurance fund up
            desc_parts.append(f"Bima {amt:,}")
        elif cat == "akiba":
            lines.append(("cash", amt, 0))
            lines.append((f"akiba:{mid}", 0, amt))    # savings up
            desc_parts.append(f"Akiba {amt:,}")
        else:
            raise AppError(f"Aina ya mchango '{cat}' haijulikani. Tumia: hisa, jamii, bima, au akiba.")

    if not lines:
        raise AppError("Hakuna kiasi cha kupitisha.")

    total = sum(a for a in amounts.values() if int(a) > 0)
    description = f"{member['name']}: {', '.join(desc_parts)}"
    if mpesa_ref:
        description += f" [M-Pesa: {mpesa_ref}]"

    jid = ledger.post_journal(
        conn,
        tx_date=today_str,
        kind="contribute",
        description=description,
        actor=actor,
        member_id=mid,
        mpesa_ref=mpesa_ref,
        lines=lines,
        idempotency_key=idem_key,
    )
    receipt = {
        "ok": True,
        "action": "contribute",
        "message": f"✅ {description} = {total:,} TSH.",
        "journal_id": jid,
        "member_name": member["name"],
        "member_no": member["member_no"],
        "member_phone": member["phone"],
        "mpesa_ref": mpesa_ref,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Fees (the review's Model A: cash in, income up — no balance deduction) ─

def commit_fee(
    conn: sqlite3.Connection, *, amount: int, actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    if amount <= 0:
        raise AppError("Kiasi cha ada kiwe chanya.")
    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="fee",
        description=f"Ada ya kikundi {amount:,}",
        actor=actor,
        lines=[
            ("cash", amount, 0),
            ("income:ada", 0, amount),
        ],
        idempotency_key=idem_key,
    )
    receipt = {
        "ok": True,
        "action": "fee",
        "message": f"✅ Ada {amount:,} TSH imerekodiwa.",
        "journal_id": jid,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Fines (cash in, income up — same Model A fix) ───────────────────────

def commit_fine(
    conn: sqlite3.Connection, *, member_name: str, amount: int,
    actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    members.must_be_active(member)
    if amount <= 0:
        raise AppError("Kiasi cha faini kiwe chanya.")
    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="fine",
        description=f"Faini ya {member['name']} {amount:,}",
        actor=actor,
        member_id=member["id"],
        lines=[
            ("cash", amount, 0),
            ("income:faini", 0, amount),
        ],
        idempotency_key=idem_key,
    )
    receipt = {
        "ok": True,
        "action": "fine",
        "message": f"✅ Faini ya {member['name']} {amount:,} TSH.",
        "journal_id": jid,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Loan issue (with eligibility, cash-available, and guarantor checks) ─

def commit_loan(
    conn: sqlite3.Connection, *, member_name: str, amount: int,
    guarantors: Optional[list], actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    members.must_be_active(member)
    mid = member["id"]

    if amount <= 0:
        raise AppError("Kiasi cha mkopo kiwe chanya.")

    # Credit risk: max borrow = eligibility_multiple x hisa balance
    hisa_bal = ledger.display_balance(conn, f"hisa:{mid}")
    multiple = _setting_int(conn, "eligibility_multiple") or 3
    max_loan = hisa_bal * multiple
    outstanding = int(
        conn.execute(
            "SELECT COALESCE(SUM(total_due - amount_paid), 0) "
            "FROM loans WHERE member_id=? AND status='active'",
            (mid,),
        ).fetchone()[0]
    )
    available_credit = max_loan - outstanding
    if amount > available_credit:
        raise AppError(
            f"Mkopo wa {amount:,} TSH upitaji mdogo. {member['name']} ana hisa {hisa_bal:,}, "
            f"cheo cha {multiple}x = {max_loan:,}, deni la sasa {outstanding:,}. "
            f"Bakia: {available_credit:,} TSH."
        )

    # Cash available to lend
    cash = ledger.raw_balance(conn, "cash")
    if amount > cash:
        raise AppError(
            f"Fedha tasani ni {cash:,} TSH tu — haitoshi kwa mkopo wa {amount:,} TSH."
        )

    # Guarantors
    guarantor_ids = []
    if _setting_int(conn, "require_guarantors") and (guarantors is None or len(guarantors) < 1):
        raise AppError("Mkopo unahitaji wadhamini. Ongeza: wadhamini ni Juma, Amina")
    if guarantors:
        for g_name in guarantors:
            g = members.resolve(conn, g_name)
            if g["id"] == mid:
                raise AppError(f"{g_name} hawezi kuwa mdhamini wa mkopo wake mwenyewe.")
            members.must_be_active(g)
            guarantor_ids.append(g["id"])

    rate_pct = _setting_int(conn, "interest_rate_pct") or 10
    rate = rate_pct / 100.0
    weeks = _setting_int(conn, "loan_weeks") or 12
    total_due = int(amount * (1 + rate))  # flat interest
    due_date = (date.fromisoformat(config.today()) + timedelta(weeks=weeks)).isoformat()

    # Ensure the loan receivable sub-account exists
    ledger.ensure_account(conn, f"loan:{mid}", f"Mkopo wa {member['name']}", "asset")

    # Journal: cash out, loan receivable up
    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="loan",
        description=f"Mkopo wa {member['name']} {amount:,} (riba {rate_pct}%)",
        actor=actor,
        member_id=mid,
        lines=[
            (f"loan:{mid}", amount, 0),  # receivable
            ("cash", 0, amount),            # cash out
        ],
        idempotency_key=idem_key,
    )

    cur = conn.execute(
        "INSERT INTO loans(member_id, principal, rate, total_due, amount_paid, issue_date, due_date, journal_id) "
        "VALUES(?, ?, ?, ?, 0, ?, ?, ?)",
        (mid, amount, rate, total_due, config.today(), due_date, jid),
    )
    loan_id = cur.lastrowid

    for gid in guarantor_ids:
        conn.execute(
            "INSERT INTO loan_guarantors(loan_id, member_id) VALUES(?, ?)",
            (loan_id, gid),
        )

    receipt = {
        "ok": True,
        "action": "loan",
        "message": (
            f"✅ Mkopo wa {amount:,} TSH kwa {member['name']}. "
            f"Riba {rate_pct}% = {total_due:,} TSH. Kurudi {due_date}."
        ),
        "journal_id": jid,
        "loan_id": loan_id,
        "total_due": total_due,
        "due_date": due_date,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Loan repayment (principal vs interest split — gawio depends on this) ──

def commit_repay(
    conn: sqlite3.Connection, *, member_name: str, amount: int,
    actor: str, idem_key: str, mpesa_ref: Optional[str] = None,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    mid = member["id"]

    if amount <= 0:
        raise AppError("Kiasi cha rejesho kiwe chanya.")

    loan = conn.execute(
        "SELECT * FROM loans WHERE member_id=? AND status='active' ORDER BY id ASC LIMIT 1",
        (mid,),
    ).fetchone()
    if not loan:
        raise AppError(f"{member['name']} hakuna mkopo hai.")

    remaining = loan["total_due"] - loan["amount_paid"]
    actual = min(amount, remaining)
    loan_id = loan["id"]

    # Split: interest (riba) first, then principal
    interest_portion = loan["total_due"] - loan["principal"]
    interest_already = min(loan["amount_paid"], interest_portion)
    interest_remaining = interest_portion - interest_already

    riba = min(actual, interest_remaining)
    principal = actual - riba

    lines = [
        ("cash", actual, 0),                    # cash in
        (f"loan:{mid}", 0, principal),           # receivable down
    ]
    if riba > 0:
        lines.append(("income:riba", 0, riba))  # interest income

    desc = f"Rejesho la {member['name']} {actual:,} (rasmi {principal:,}, riba {riba:,})"
    if mpesa_ref:
        desc += f" [M-Pesa: {mpesa_ref}]"

    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="repay",
        description=desc,
        actor=actor,
        member_id=mid,
        mpesa_ref=mpesa_ref,
        lines=lines,
        idempotency_key=idem_key,
    )

    new_paid = loan["amount_paid"] + actual
    if new_paid >= loan["total_due"]:
        conn.execute("UPDATE loans SET amount_paid=?, status='closed' WHERE id=?", (new_paid, loan_id))
        status_msg = " Mkopo umekamilika! 🎉"
    else:
        conn.execute("UPDATE loans SET amount_paid=? WHERE id=?", (new_paid, loan_id))
        still = loan["total_due"] - new_paid
        status_msg = f" Baki: {still:,} TSH."

    receipt = {
        "ok": True,
        "action": "repay",
        "message": f"✅ Rejesho {actual:,} TSH kwa {member['name']}.{status_msg}",
        "journal_id": jid,
        "loan_id": loan_id,
        "principal_portion": principal,
        "interest_portion": riba,
        "remaining": loan["total_due"] - new_paid,
        "member_name": member["name"],
        "member_no": member["member_no"],
        "member_phone": member["phone"],
        "mpesa_ref": mpesa_ref,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Payouts (jamii/bima disbursements — the missing outflow path) ────────

def commit_payout(
    conn: sqlite3.Connection, *, member_name: str, amount: int,
    actor: str, idem_key: str, fund: str = "jamii",
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    if amount <= 0:
        raise AppError("Kiasi cha tozo kiwe chanya.")

    fund_code = fund if fund in ("jamii", "bima") else "jamii"
    fund_balance = ledger.display_balance(conn, fund_code)
    if amount > fund_balance:
        raise AppError(
            f"Mfuko wa {fund_code} una {fund_balance:,} TSH tu — haitoshi kwa tozo ya {amount:,}."
        )
    cash = ledger.raw_balance(conn, "cash")
    if amount > cash:
        raise AppError(f"Fedha tasani ni {cash:,} TSH tu — haitoshi.")

    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="payout",
        description=f"Tozo ya {fund_code} kwa {member['name']} {amount:,}",
        actor=actor,
        member_id=member["id"],
        lines=[
            (fund_code, amount, 0),   # fund down
            ("cash", 0, amount),       # cash out
        ],
        idempotency_key=idem_key,
    )
    receipt = {
        "ok": True,
        "action": "payout",
        "message": f"✅ {fund_code} {amount:,} TSH imetumwa kwa {member['name']}.",
        "journal_id": jid,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Group expense ───────────────────────────────────────────────────────

def commit_expense(
    conn: sqlite3.Connection, *, amount: int, description: str,
    actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    if amount <= 0:
        raise AppError("Kiasi cha matumizi kiwe chanya.")
    cash = ledger.raw_balance(conn, "cash")
    if amount > cash:
        raise AppError(f"Fedha tasani ni {cash:,} TSH tu — haitoshi kwa matumizi.")
    jid = ledger.post_journal(
        conn,
        tx_date=config.today(),
        kind="expense",
        description=description[:200],
        actor=actor,
        lines=[
            ("expense:matumizi", amount, 0),
            ("cash", 0, amount),
        ],
        idempotency_key=idem_key,
    )
    receipt = {
        "ok": True,
        "action": "expense",
        "message": f"✅ Matumizi {amount:,} TSH: {description[:80]}",
        "journal_id": jid,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt


# ── Member Exit Settlement (refundable hisa + akiba) ────────────────────

def commit_exit(
    conn: sqlite3.Connection, *, member_name: str, actor: str, idem_key: str,
) -> dict:
    _check_idempotency(conn, idem_key)
    member = members.resolve(conn, member_name)
    members.must_be_active(member)
    mid = member["id"]

    settlement = members.exit_settlement(conn, member)
    if not settlement["can_exit"]:
        raise AppError(settlement["note"])

    hisa_bal = settlement["hisa"]
    akiba_bal = settlement["akiba"]
    total_refund = settlement["payable"]

    lines = []
    if hisa_bal > 0:
        lines.append((f"hisa:{mid}", hisa_bal, 0))    # liability down
    if akiba_bal > 0:
        lines.append((f"akiba:{mid}", akiba_bal, 0))   # liability down

    if total_refund > 0:
        cash = ledger.raw_balance(conn, "cash")
        if cash < total_refund:
            raise AppError(f"Fedha tasani ni {cash:,} TSH tu — haitoshi kurejesha {total_refund:,} TSH.")
        lines.append(("cash", 0, total_refund))       # cash out

    jid = None
    if lines:
        jid = ledger.post_journal(
            conn,
            tx_date=config.today(),
            kind="exit",
            description=f"Kutoka kwa mwanachama {member['name']} ({member['member_no']}) — marejesho {total_refund:,} TSH",
            actor=actor,
            member_id=mid,
            lines=lines,
            idempotency_key=idem_key,
        )

    conn.execute(
        "UPDATE members SET status='exited', exit_date=? WHERE id=?",
        (config.today(), mid),
    )

    msg = f"✅ Mwanachama {member['name']} ({member['member_no']}) ametoka."
    if total_refund > 0:
        msg += f" Amerejeshewa Hisa: {hisa_bal:,} TSH na Akiba: {akiba_bal:,} TSH (Jumla {total_refund:,} TSH)."
    else:
        msg += " Hakuna marejesho ya hisa/akiba."

    receipt = {
        "ok": True,
        "action": "exit",
        "message": msg,
        "journal_id": jid,
        "member_id": mid,
        "refund_hisa": hisa_bal,
        "refund_akiba": akiba_bal,
        "total_refund": total_refund,
    }
    _log_commit(conn, idem_key, receipt)
    return receipt
