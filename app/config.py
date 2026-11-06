import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'niagara.db'}")
    # "gemini" uses the real LLM; "heuristic" runs fully offline (tests, eval baseline, demo without key).
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini" if os.getenv("GEMINI_API_KEY") else "heuristic")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    company_name: str = os.getenv("COMPANY_NAME", "CV Maju Sejahtera Distribusi")
    media_dir: Path = ROOT / "media"


settings = Settings()
settings.media_dir.mkdir(exist_ok=True)
