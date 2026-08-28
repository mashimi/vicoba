# 💰 VICOBA Digital Treasurer (Mhazinaji Mtandao)

A production-ready, Swahili-first digital treasurer web application for VICOBA (Village Community Banks). Built with Python, FastAPI, SQLite, and Vanilla HTML/CSS/JS.

## 🌟 Key Features

1. **Two-Phase Transaction Architecture**:
   - **Phase 1 (`POST /parse`)**: Rule-based (or Optional Claude LLM) parser parses free-form Swahili text into a structured intent with zero side effects.
   - **Phase 2 (`POST /commit`)**: Atomic, deterministic Python commit engine executes the validated intent against the double-entry accounting ledger.

2. **Double-Entry Accounting Ledger Engine**:
   - Strictly enforces the fundamental identity:
     $$\text{Cash} + \text{Loans} = \text{Hisa} + \text{Akiba} + \text{Jamii} + \text{Bima} + \text{Net Income}$$
   - Zero "vanishing money" bugs: fees and fines are cash-in/income-up, never silent deductions from member savings.

3. **Member Management & Registration**:
   - Member numbers (BSDA-001, BSDA-002, ...) as unique primary identity to solve ambiguous name duplicates (the "Two-Asha" problem).
   - Member exit settlement: refunds Hisa + Akiba, enforces zero active loan balance before exit.

4. **Rich & Modern Swahili Web UI**:
   - Responsive mobile-first dashboard with real-time financial metrics.
   - Instant transaction preview cards with interactive confirmation.
   - Live member search and statement history drawer.
   - Profit distribution (Gawio Estimate) calculator.
   - Meeting sheet report & unpaid members tracker.
   - CSV export for auditors.

---

## 🚀 Getting Started

### 1. Requirements
- Python 3.10+
- Runtime: `fastapi`, `uvicorn`, `jinja2`, `httpx`, `python-multipart`
- Development: `pytest`, `ruff`

### 2. Install Dependencies
```bash
pip install -r requirements.txt            # runtime
pip install -r requirements-dev.txt        # + test & lint tooling
```

### 3. Run the Application
```bash
python -m app.main
```
Or with uvicorn directly:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Or with Docker (database persisted on the `/data` volume):
```bash
docker build -t vicoba .
docker run -p 8000:8000 -v vicoba-data:/data vicoba
```
Open your browser at `http://localhost:8000`.

### 4. Running Tests
```bash
pytest
```

---

## 🗣️ Example Swahili Commands

| Intent | Example Swahili Text |
| :--- | :--- |
| **Usajili (Register)** | `msajili Juma Ally 0712345678` |
| **Mchango (Contribute)** | `Amina amelipa hisa 5000, jamii 1000, bima 2000` |
| **Mkopo (Loan)** | `Juma kopa 50000 wadhamini ni Fatuma` |
| **Rejesho (Repay)** | `Juma amelipa mkopo 10000` |
| **Ada (Fee)** | `ada ya kikundi 2000` |
| **Faini (Fine)** | `faini ya Amina 1000` |
| **Kutoka (Exit)** | `ondoa mwanachama Juma Ally` |
| **Taarifa (Statement)** | `taarifa ya Juma` |

---

## 🔒 Security & Verification
- PIN authentication with **PBKDF2-HMAC-SHA256** (100k iterations, per-user random salt).
  Legacy unsalted-SHA256 rows are transparently upgraded at the user's next successful login.
- **Server-side sessions**: the cookie holds a random opaque token (HttpOnly, SameSite=Strict,
  Secure on HTTPS); only the token's SHA-256 is stored in the database, so a leaked database
  cannot be replayed as live sessions. PIN changes revoke all existing sessions.
- **Login rate limiting**: 5 failed attempts per 10 minutes per IP → 429 lockout window.
- **RBAC on every data endpoint**: statements, group position, meeting sheet, unpaid tracker,
  gawio, member list, CSV export, settings and the exit-settlement preview all require a
  logged-in committee member; `/commit` requires mhazinaji+; user management requires mwenyekiti.
- Idempotency key tracking on all commit operations.
- Health endpoint (`/api/health`) for instant double-entry ledger balance verification.
- **Backups**: `python scripts/backup_db.py --keep 14` takes a WAL-safe online snapshot of the
  database (schedule it daily — this file holds the group's money records).
