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
- Dependencies: `fastapi`, `uvicorn`, `jinja2`, `httpx`, `pytest`, `python-multipart`

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the Application
```bash
python -m app.main
```
Or with uvicorn directly:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
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
- PIN Authentication with HMAC SHA-256 password hashing.
- Idempotency key tracking on all commit operations.
- Health endpoint (`/api/health`) for instant double-entry ledger balance verification.
