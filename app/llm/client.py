"""LLM facade: Gemini with a fallback model, then the offline heuristic. Callers never see a provider error."""

import json
import mimetypes
from pathlib import Path

from app import tracing
from app.config import settings
from app.llm import heuristic
from app.llm.prompts import RERANK_SYSTEM, UNDERSTAND_SYSTEM
from app.llm.schemas import RerankResult, Understanding
from app.privacy import redact


class LLMService:
    def __init__(self, provider: str | None = None):
        self.provider = provider or settings.llm_provider
        self._client = None
        if self.provider == "gemini":
            from google import genai

            self._client = genai.Client(api_key=settings.gemini_api_key)

    @property
    def mode(self) -> str:
        return self.provider

    def _generate(self, system: str, parts: list, schema):
        from google.genai import types

        config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                             response_schema=schema, temperature=0)
        last_exc = None
        for model in (settings.gemini_model, settings.gemini_fallback_model):
            try:
                with tracing.Timer() as t:
                    resp = self._client.models.generate_content(model=model, contents=parts, config=config)
                tracing.count_llm_call()
                parsed = resp.parsed if resp.parsed is not None else schema.model_validate_json(resp.text)
                return parsed, model, t.ms
            except Exception as exc:  # network, quota, or schema errors: try the next model
                last_exc = exc
                tracing.step("error", f"LLM {model} gagal, mencoba cadangan", {"error": str(exc)[:300]}, ok=False)
        raise RuntimeError(f"Semua model gagal: {last_exc}")

    def understand(self, text: str, kind: str = "text", media_path: str | None = None, ctx: dict | None = None) -> tuple[Understanding, str]:
        ctx = ctx or {}
        if self.provider != "gemini":
            return heuristic.understand(text, kind, media_path, ctx), "heuristic"
        from google.genai import types

        raw_text = text
        text, found = redact(text or "", ctx.get("pii_names"))
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
        parts: list = [f"Konteks: {json.dumps(context, ensure_ascii=False)}\n<pesan>{text or ''}</pesan>"]
        if media_path and kind in ("image", "audio"):
            mime = mimetypes.guess_type(media_path)[0] or ("image/png" if kind == "image" else "audio/ogg")
            parts.append(types.Part.from_bytes(data=Path(media_path).read_bytes(), mime_type=mime))
        try:
            parsed, model, ms = self._generate(UNDERSTAND_SYSTEM, parts, Understanding)
            return parsed, model
        except Exception as exc:
            tracing.step("error", "LLM tidak tersedia, memakai parser offline", {"error": str(exc)[:300]}, ok=False)
            return heuristic.understand(raw_text, kind, media_path, ctx), "heuristic"

    def rerank(self, raw: str, candidates: list[dict], usual_ids: set[int]) -> tuple[RerankResult, str]:
        if self.provider != "gemini" or not candidates:
            return heuristic.rerank(raw, candidates, usual_ids), "heuristic"
        listing = "\n".join(
            f"{i}. {c['name']} (skor kemiripan {c['score']:.0f}{', sering dibeli pelanggan ini' if c['product_id'] in usual_ids else ''})"
            for i, c in enumerate(candidates)
        )
        prompt = f"Tulisan pelanggan: <pesan>{raw}</pesan>\nKandidat:\n{listing}"
        try:
            parsed, model, ms = self._generate(RERANK_SYSTEM, [prompt], RerankResult)
            if parsed.choice >= len(candidates):
                parsed.choice = -1
            return parsed, model
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
