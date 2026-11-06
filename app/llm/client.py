"""LLM facade: a chain of providers (for example Groq, then Gemini), then the offline parser.
A provider that hits its rate limit is skipped for a while instead of being retried on every message.
Callers never see a provider error."""

import json

from app import tracing
from app.config import settings
from app.llm import heuristic
from app.llm.prompts import RERANK_SYSTEM, UNDERSTAND_SYSTEM
from app.llm.providers import GeminiProvider, OpenAICompatProvider, Provider, RateLimited, Unsupported
from app.llm.schemas import RerankResult, Understanding
from app.privacy import redact


def build_providers(only: str | None = None) -> list[Provider]:
    built: dict[str, Provider] = {}
    if settings.compat_api_key or settings.compat_base_url.startswith("http://localhost"):
        built["compat"] = OpenAICompatProvider(settings.compat_name, settings.compat_base_url, settings.compat_api_key,
                                               settings.compat_model, settings.compat_vision_model,
                                               settings.compat_audio_model, settings.llm_timeout_seconds)
    if settings.gemini_api_key:
        built["gemini"] = GeminiProvider(settings.gemini_api_key, [settings.gemini_model, settings.gemini_fallback_model],
                                         settings.llm_timeout_seconds)
    order = [only] if only else [p.strip() for p in settings.llm_chain.split(",") if p.strip()]
    return [built[name] for name in order if name in built]


class LLMService:
    def __init__(self, provider: str | None = None, providers: list[Provider] | None = None):
        choice = provider or settings.llm_provider
        if providers is not None:
            self.providers = providers
        elif choice == "heuristic":
            self.providers = []
        else:
            self.providers = build_providers(choice if choice in ("compat", "gemini") else None)

    @property
    def mode(self) -> str:
        return "llm" if self.providers else "heuristic"

    @property
    def label(self) -> str:
        return " lalu ".join(p.label for p in self.providers) if self.providers else "parser offline"

    def status(self) -> list[dict]:
        return [p.status() for p in self.providers]

    def _generate(self, system: str, text: str, schema, media_path: str | None = None, kind: str = "text"):
        tried = []
        for p in self.providers:
            if p.cooling():
                tried.append(f"{p.name}: jeda karena limit")
                continue
            try:
                with tracing.Timer() as t:
                    parsed = p.generate(system, text, schema, media_path, kind)
                tracing.count_llm_call()
                p.last_error = None
                return parsed, p.label, t.ms
            except RateLimited as exc:
                p.cool_down(exc.retry_after or 60)
                p.last_error = str(exc)[:200]
                tried.append(f"{p.name}: limit, dijeda {int(exc.retry_after or 60)} detik")
            except Unsupported as exc:
                tried.append(str(exc))
            except Exception as exc:
                p.last_error = str(exc)[:200]
                tried.append(f"{p.name}: {str(exc)[:160]}")
            tracing.step("error", f"{p.label} tidak bisa dipakai, mencoba berikutnya", {"alasan": tried[-1]}, ok=False)
        raise RuntimeError("; ".join(tried) or "tidak ada penyedia LLM")

    def understand(self, text: str, kind: str = "text", media_path: str | None = None,
                   ctx: dict | None = None) -> tuple[Understanding, str]:
        ctx = ctx or {}
        if not self.providers:
            return heuristic.understand(text, kind, media_path, ctx), "heuristic"
        masked, found = redact(text or "", ctx.get("pii_names"))
        if found:
            tracing.step("policy", "Data pribadi disamarkan sebelum dikirim ke LLM",
                         {"jumlah_per_jenis": found, "catatan": "Nilai aslinya tidak ikut dikirim maupun dicatat di sini."})
        context = {
            "tanggal_hari_ini": str(ctx.get("today")),
            "hari": ctx.get("weekday"),
            "ada_pertanyaan_tertunda": bool(ctx.get("pending_question")),
            "pertanyaan_tertunda": redact(ctx.get("pending_question_text") or "", ctx.get("pii_names"))[0] or None,
            "sedang_ditagih": bool(ctx.get("in_collection")),
            "ada_order_terbuka": bool(ctx.get("has_open_order")),
        }
        prompt = f"Konteks: {json.dumps(context, ensure_ascii=False)}\n<pesan>{masked}</pesan>"
        try:
            parsed, label, _ = self._generate(UNDERSTAND_SYSTEM, prompt, Understanding, media_path, kind)
            return parsed, label
        except Exception as exc:
            tracing.step("error", "Semua LLM tidak tersedia, memakai parser offline", {"alasan": str(exc)[:400]}, ok=False)
            return heuristic.understand(text, kind, media_path, ctx), "heuristic"

    def rerank(self, raw: str, candidates: list[dict], usual_ids: set[int]) -> tuple[RerankResult, str]:
        if not self.providers or not candidates:
            return heuristic.rerank(raw, candidates, usual_ids), "heuristic"
        listing = "\n".join(
            f"{i}. {c['name']} (skor kemiripan {c['score']:.0f}{', sering dibeli pelanggan ini' if c['product_id'] in usual_ids else ''})"
            for i, c in enumerate(candidates)
        )
        prompt = f"Tulisan pelanggan: <pesan>{raw}</pesan>\nKandidat:\n{listing}"
        try:
            parsed, label, _ = self._generate(RERANK_SYSTEM, prompt, RerankResult)
            if parsed.choice >= len(candidates):
                parsed.choice = -1
            return parsed, label
        except Exception:
            return heuristic.rerank(raw, candidates, usual_ids), "heuristic"


_service: LLMService | None = None


def get_llm() -> LLMService:
    global _service
    if _service is None:
        _service = LLMService()
    return _service


def set_llm(service: LLMService) -> None:
    global _service
    _service = service
