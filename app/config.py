"""Runtime configuration. Everything is read lazily from the environment so
tests can point VICOBA_DB at a temp file before the app starts."""
import os
import secrets
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def db_path() -> Path:
    return Path(os.environ.get("VICOBA_DB", BASE_DIR / "vicoba.db"))


def group_name() -> str:
    return os.environ.get("GROUP_NAME", "Kikundi cha VICOBA")


def anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def local_llm_url() -> str:
    return os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1/chat/completions")


def local_llm_model() -> str:
    return os.environ.get("LOCAL_LLM_MODEL", "cactus")


def llm_provider() -> str:
    if anthropic_key():
        return os.environ.get("LLM_PROVIDER", "anthropic")
    return os.environ.get("LLM_PROVIDER", "local")


def llm_enabled() -> bool:
    val = os.environ.get("USE_LLM_PARSER", "1").lower()
    return val not in ("0", "false", "off", "no")


def llm_model() -> str:
    if llm_provider() == "anthropic":
        return os.environ.get("LLM_MODEL", "claude-haiku-4-5")
    return local_llm_model()


def treasurer_pin() -> str:
    """Optional PIN supplied via env; if unset, the first login sets it."""
    return os.environ.get("TREASURER_PIN", "")


# ── OpenWA (WhatsApp gateway) integration ────────────────────────────────


def openwa_url() -> str:
    """Base URL of the OpenWA WhatsApp API Gateway (default is Docker default)."""
    return os.environ.get("OPENWA_URL", "http://localhost:2785")


def openwa_api_key() -> str:
    """OpenWA API key sent as `X-API-Key` on send-text requests."""
    return os.environ.get("OPENWA_API_KEY", "")


def openwa_session_id() -> str:
    """OpenWA session used when auto-replying on WhatsApp."""
    return os.environ.get("OPENWA_SESSION_ID", "")


def openwa_webhook_secret() -> str:
    """Shared secret OpenWA signs webhook bodies with (`X-OpenWA-Signature`).

    Empty string disables signature verification (handy for local testing,
    but always set it in production)."""
    return os.environ.get("OPENWA_WEBHOOK_SECRET", "")


def openwa_treasurer_numbers() -> list:
    """WhatsApp phone numbers allowed to run privileged commands
    (register, expense, exit, payout). Digits only, comma-separated:
    OPENWA_TREASURER_NUMBERS=0712345678,0755123456"""
    raw = os.environ.get("OPENWA_TREASURER_NUMBERS", "")
    return [n.strip() for n in raw.split(",") if n.strip()]





def today() -> str:
    return date.today().isoformat()


def new_secret() -> str:
    return secrets.token_hex(32)
