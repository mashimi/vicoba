"""Rule-based Swahili parser: turns a free-text sentence into a structured intent.

This is the `/parse` half of the two-phase architecture. It returns a ParsedIntent
that the UI renders as a confirmation preview — the user taps [Thibitisha]
before any money moves. No DB writes happen here.

When USE_LLM_PARSER=1 and ANTHROPIC_API_KEY are set, llm_parser.parse() can be
swapped in behind the same interface for broader language coverage.
"""
import re
from dataclasses import dataclass
from typing import Optional

# Amount: "5000", "5,000" (no bare trailing commas/spaces)
AMT = r"(\d+(?:,\d{3})*)"
# Person name: starts with a letter, may contain spaces/apostrophes, lazy
NAME = r"([a-z\u00C0-\u024F][a-z\u00C0-\u024F' ]{1,39}?)"

# Domain vocabulary — never part of a person's name
DOMAIN_WORDS = {
    "mkopo", "deni", "hisa", "jamii", "bima", "akiba", "ada", "faini", "riba",
    "rejesho", "rejesha", "mchango", "msaada", "msiba", "mfuko", "fedha",
    "kikundi", "kutaniko", "mwanachama", "mapato", "matumizi", "gawio",
    "wadhamini", "madhamini", "leo", "mwezi", "wiki",
}

CONTRIBUTE_VERBS = (
    "amelipa|alilipa|analiipa|amechangia|anachangia|alichangia|ameleta|analeta|"
    "ameweka|aliweka|anaweka|amedunga|lipa|lipe|changia|leta|weka"
)
REPAY_VERBS = "amelipa|alilipa|analiipa|rejesha|rejesho|maliza|lipa|repay"
LOAN_VERBS = (
    "amekopa|alikopa|anakopa|kopa|amekopesh\\w*|alikopesh\\w*|"
    "tulimkopa|ametolea|alitolea|toa|kopesha"
)
CATEGORIES = ("hisa", "akiba", "jamii", "bima")
LOAN_CONTEXT = ("mkopo", "deni", "rejesho", "rejesha", "kopa", "kopesha")


@dataclass
class ParsedIntent:
    action: str
    member: Optional[str] = None
    member_no: Optional[str] = None
    amounts: Optional[dict] = None  # {hisa: 5000, jamii: 1000, ...}
    amount: int = 0
    loan_id: Optional[int] = None
    guarantors: Optional[list] = None
    mpesa_ref: Optional[str] = None
    description: str = ""


def parse(text: str) -> ParsedIntent:
    intent = _parse_internal(text)
    if intent.action not in ("unknown", "member_statement", "who_unpaid", "group_position"):
        if not intent.mpesa_ref:
            intent.mpesa_ref = _extract_mpesa_ref(text)
    return intent


def _parse_internal(text: str) -> ParsedIntent:
    t = normalize(text)
    words = set(re.findall(r"[a-z\u00C0-\u024F']+", t))

    # ── 1. Registration ──────────────────────────────────────────────
    m = re.search(
        rf"(?:msajili|sajili|andikisha|register|ongeza)\s+"
        rf"(?:mwanachama\s+)?(?:kwa jina\s+|anayeitwa\s+|jina\s+|ni\s+)?"
        rf"{NAME}(?=\s*\d|\s*$)",
        t,
    )
    if m:
        name = clean_name(m.group(1))
        if name:
            phone = _extract_phone(text)
            return ParsedIntent(
                action="register", member=name,
                amounts={"phone": phone} if phone else None,
                description=f"Msajili {name}",
            )

    # ── 2. Fine ──────────────────────────────────────────────────────
    m = re.search(rf"\bfaini\b\s*(?:ya\s+|kwa\s+|za\s+|ni\s+)?{NAME}\s+{AMT}", t)
    if not m:
        m = re.search(rf"^{NAME}\s+(?:a|me|li)?fainiwa\s+{AMT}", t)
    if m and len(m.groups()) >= 2:
        name = clean_name(m.group(1))
        if name:
            return ParsedIntent(
                action="fine", member=name, amount=_parse_amount(m.group(2)),
                description=f"Faini ya {name}",
            )

    # ── 3. Fee (ada) or group expense (matumizi) ────────────────────
    m = re.search(rf"\bada\b[^0-9]*{AMT}", t)
    if m:
        return ParsedIntent(
            action="fee", amount=_parse_amount(m.group(1)),
            description="Ada ya kikundi",
        )
    m = re.search(rf"\b(?:tulitumia|matumizi|tumia)\b\s*(?:fedha\s+)?(?:ya\s+)?{AMT}", t)
    if m:
        return ParsedIntent(
            action="expense", amount=_parse_amount(m.group(1)),
            description=text[:120],
        )

    # ── 4. Payout from jamii/bima funds ──────────────────────────────
    is_payout = ({"msaada", "msiba", "tulituma", "tuma"} & words) and (
        {"jamii", "bima", "mfuko", "msaada"} & words
    )
    if is_payout:
        fund = "bima" if "bima" in words else "jamii"
        member = None
        m = re.search(rf"\bkwa\s+{NAME}(?=\s|$)", t)
        if m:
            member = clean_name(m.group(1))
        if not member:
            m = re.search(rf"(?:tulim|alim|amem|nim)\w*\s+{NAME}\s", t)
            if m:
                member = clean_name(m.group(1))
        m = re.search(AMT, t)
        amount = _parse_amount(m.group(1)) if m else 0
        if amount > 0:
            return ParsedIntent(
                action="payout", member=member or "", amount=amount,
                description=f"Tozo kutoka mfuko wa {fund}",
            )

    # ── 5. Contribution (name + verb + category amounts) ────────────
    m = re.search(rf"^{NAME}\s+(?:{CONTRIBUTE_VERBS})\b", t)
    if m:
        name = clean_name(m.group(1))
        if name:
            amounts = {}
            for cat in CATEGORIES:
                am = re.search(rf"\b{cat}\b\s*(?:ya\s+|za\s+)?{AMT}", t)
                if am:
                    amounts[cat] = _parse_amount(am.group(1))
            if amounts:
                total = sum(amounts.values())
                parts = [f"{k} {v:,}" for k, v in amounts.items()]
                return ParsedIntent(
                    action="contribute", member=name, amounts=amounts,
                    amount=total, description=f"Mchango: {', '.join(parts)}",
                )
            # Bare amount after the name (no category) → hisa, but only when
            # the sentence has no loan/repay vocabulary
            if not any(w in words for w in LOAN_CONTEXT):
                am = re.search(rf"\b{name.split()[0].lower()}\b.*?{AMT}", t)
                if am:
                    val = _parse_amount(am.group(1))
                    return ParsedIntent(
                        action="contribute", member=name,
                        amounts={"hisa": val}, amount=val,
                        description=f"Mchango: hisa {val:,}",
                    )

    # ── 6. Loan repayment ────────────────────────────────────────────
    wants_repay = bool({"rejesho", "rejesha", "maliza"} & words) or (
        "lipa" in t and bool({"mkopo", "deni"} & words)
    )
    if wants_repay:
        m = re.search(
            rf"^{NAME}\s+(?:{REPAY_VERBS})\b\s+(?:mkopo\s+|deni\s+|rejesho\s+)?{AMT}", t
        )
        if not m:
            m = re.search(rf"(?:rejesho|rejesha)\s+(?:la\s+|ya\s+)?{NAME}\s+{AMT}", t)
        if m:
            name = clean_name(m.group(1))
            if name:
                return ParsedIntent(
                    action="repay", member=name, amount=_parse_amount(m.group(2)),
                    description=f"Rejesho la mkopo — {name}",
                )

    # ── 7. Loan issue ────────────────────────────────────────────────
    if {"mkopo", "kopa", "kopesha", "loan"} & words:
        m = re.search(rf"^{NAME}\s+(?:{LOAN_VERBS})\b(?:\s+mkopo)?\s+{AMT}", t)
        if not m:
            m = re.search(rf"(?:kopesha|mkopesha)\s+{NAME}\s+{AMT}", t)
        if not m:
            m = re.search(rf"\bmkopo\b\s+(?:wa\s+)?{AMT}\s+(?:kwa\s+)?{NAME}(?=\s|$)", t)
        if m:
            name = clean_name(m.group(1))
            if name:
                guarantors = _extract_guarantors(t)
                return ParsedIntent(
                    action="loan", member=name, amount=_parse_amount(m.group(2)),
                    guarantors=guarantors or None,
                    description=f"Tozo la mkopo — {name}",
                )

    # ── 8. Member exit settlement ────────────────────────────────────
    if {"exit", "kutoka", "ondoa", "futa"} & words or "ametoka" in t or "anataka kutoka" in t:
        m = re.search(
            rf"(?:mwanachama\s+)?{NAME}\s+(?:ametoka|anataka kutoka|exit)", t
        )
        if not m:
            m = re.search(
                rf"(?:exit|kutoka|ondoa|futa)\s+(?:mwanachama\s+)?{NAME}(?=\s*\d|\s*$)", t
            )
        if m:
            name = clean_name(m.group(1))
            if name:
                return ParsedIntent(
                    action="exit", member=name,
                    description=f"Kutoka kwa mwanachama {name}",
                )

    # ── 9. Queries (no digits allowed — those are mutations) ────────
    if not re.search(r"\d", t):
        m = re.search(
            rf"(?:taarifa|deni|akiba|hisa|salio|mkopo|mchango|ripoti)\s+"
            rf"(?:ya|la|za|wa)\s+{NAME}(?=\s|$)",
            t,
        )
        if m:
            name = clean_name(m.group(1))
            if name and name.lower() not in ("kikundi", "kutaniko", "group"):
                return ParsedIntent(
                    action="member_statement", member=name,
                    description=f"Taarifa ya {name}",
                )
            return ParsedIntent(action="group_position", description="Maendeleo ya kikundi")

        if {"hajalipa", "hawajalipa"} & words or ("nani" in words and "lipa" in t):
            return ParsedIntent(action="who_unpaid", description="Orodha ya wasiolipa")

        if {"maendeleo", "jumla", "muhtasari", "halisi", "mapato", "gawio"} & words or "ripoti" in words:
            return ParsedIntent(action="group_position", description="Maendeleo ya kikundi")

    return ParsedIntent(action="unknown", description=text)


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace. Digits preserved (amounts, phones)."""
    return re.sub(r"\s+", " ", text.lower().strip())


def clean_name(raw: str) -> str:
    """Strip particles and domain words from a name candidate; Title-case it."""
    name = raw.strip().strip(",;.:")
    name = re.sub(r"^(?:ya|kwa|za|wa|la|na|ni|jina|mwanachama)\s+", "", name, flags=re.I)
    tokens = [w for w in name.split() if w.lower() not in DOMAIN_WORDS]
    name = " ".join(tokens)
    return name.title() if name else ""


def _parse_amount(s: str) -> int:
    return int(re.sub(r"[,\s]", "", s.strip()))


def _extract_phone(text: str) -> Optional[str]:
    m = re.search(r"\b(0[67]\d{8})\b", text)
    return m.group(1) if m else None


def _extract_guarantors(text: str) -> list:
    m = re.search(r"(?:wadhamini|madhamini)\s+(?:ni\s+|wa\s+)?(.+?)(?=\s+\d|$)", text, re.I)
    if not m:
        return []
    parts = re.split(r"(?:\s+na\s+|,|;)", m.group(1))
    return [clean_name(p) for p in parts if len(p.strip()) >= 2]


def _extract_mpesa_ref(text: str) -> Optional[str]:
    # Look for explicit prefix: ref QX84920193 or mpesa QX84920193
    m = re.search(r"\b(?:ref|mpesa|namba|kumb)\s*:?\s*([a-z0-9]{8,12})\b", text, re.I)
    if m:
        return m.group(1).upper()
    # Or standalone M-Pesa transaction code like QX84920193
    m = re.search(r"\b([A-Z][A-Z0-9]{7,11})\b", text)
    if m and not m.group(1).startswith("BSDA"):  # ignore member registration numbers
        return m.group(1)
    return None
