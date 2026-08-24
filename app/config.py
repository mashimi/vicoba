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


def today() -> str:
    return date.today().isoformat()


def new_secret() -> str:
    return secrets.token_hex(32)
