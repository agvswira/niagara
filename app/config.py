import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'niagara.db'}")
    # "heuristic" forces offline mode; anything else uses the LLM chain below when keys are set.
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto")
    # Order in which providers are tried, e.g. "compat,gemini". Providers without a key are skipped.
    llm_chain: str = os.getenv("LLM_CHAIN", "compat,gemini")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
    # Any OpenAI-compatible endpoint: Groq, OpenRouter, OpenAI, Together, or a local Ollama.
    compat_name: str = os.getenv("OPENAI_COMPAT_NAME", "groq")
    compat_base_url: str = os.getenv("OPENAI_COMPAT_BASE_URL", "https://api.groq.com/openai/v1")
    compat_api_key: str = os.getenv("OPENAI_COMPAT_API_KEY", "")
    compat_model: str = os.getenv("OPENAI_COMPAT_MODEL", "openai/gpt-oss-120b")
    compat_vision_model: str = os.getenv("OPENAI_COMPAT_VISION_MODEL", "")
    compat_audio_model: str = os.getenv("OPENAI_COMPAT_AUDIO_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    company_name: str = os.getenv("COMPANY_NAME", "CV Maju Sejahtera Distribusi")
    media_dir: Path = ROOT / "media"


settings = Settings()
settings.media_dir.mkdir(exist_ok=True)
