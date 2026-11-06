"""LLM providers behind one interface. Each returns a validated pydantic object or raises; the chain decides what next."""

import base64
import json
import mimetypes
import threading
import time
from datetime import date
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Unsupported(ProviderError):
    """The provider cannot handle this input (an image or audio without a capable model)."""


def compact_shape(schema: type[BaseModel]) -> str:
    """A short, TypeScript-like description of the output. A full JSON Schema costs about four times the tokens,
    which matters on free tiers that cap tokens per minute; pydantic still validates the reply strictly."""
    full = schema.model_json_schema()
    defs = full.get("$defs", {})

    def render(node: dict, indent: int) -> str:
        if "$ref" in node:
            node = defs[node["$ref"].split("/")[-1]]
        if "anyOf" in node:
            parts = [render(n, indent) for n in node["anyOf"] if n.get("type") != "null"]
            return " | ".join(parts) + (" | null" if any(n.get("type") == "null" for n in node["anyOf"]) else "")
        if "enum" in node:
            return " | ".join(f'"{v}"' for v in node["enum"])
        kind = node.get("type")
        if kind == "array":
            return f"[{render(node.get('items', {}), indent)}]"
        if kind == "object" or "properties" in node:
            pad = "  " * (indent + 1)
            rows = []
            for name, prop in node.get("properties", {}).items():
                note = prop.get("description")
                rows.append(f"{pad}{name}: {render(prop, indent + 1)}" + (f"  // {note}" if note else ""))
            return "{\n" + "\n".join(rows) + "\n" + "  " * indent + "}"
        return {"integer": "int", "number": "number", "string": "string", "boolean": "bool"}.get(kind, "any")

    return render(full, 0)


class Provider:
    name = "provider"

    def __init__(self):
        self._lock = threading.Lock()
        self.cooling_until = 0.0
        self.calls: dict[str, int] = {}
        self.last_error: str | None = None

    @property
    def label(self) -> str:
        return self.name

    def cooling(self) -> bool:
        return time.time() < self.cooling_until

    def cool_down(self, seconds: float) -> None:
        self.cooling_until = time.time() + max(5.0, seconds)

    def count(self) -> None:
        with self._lock:
            key = date.today().isoformat()
            self.calls[key] = self.calls.get(key, 0) + 1

    def calls_today(self) -> int:
        return self.calls.get(date.today().isoformat(), 0)

    def status(self) -> dict:
        return {"name": self.name, "label": self.label, "calls_today": self.calls_today(),
                "cooling_seconds": max(0, int(self.cooling_until - time.time())), "last_error": self.last_error}

    def generate(self, system: str, text: str, schema: type[BaseModel], media_path: str | None = None,
                 kind: str = "text") -> BaseModel:
        raise NotImplementedError


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str, models: list[str], timeout: float):
        super().__init__()
        from google import genai

        self.models = [m for m in models if m]
        self.client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})

    @property
    def label(self) -> str:
        return f"gemini {self.models[0]}"

    def generate(self, system, text, schema, media_path=None, kind="text"):
        from google.genai import errors, types

        parts: list = [text]
        if media_path and kind in ("image", "audio"):
            mime = mimetypes.guess_type(media_path)[0] or ("image/png" if kind == "image" else "audio/ogg")
            parts.append(types.Part.from_bytes(data=Path(media_path).read_bytes(), mime_type=mime))
        config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                             response_schema=schema, temperature=0)
        last: Exception | None = None
        for model in self.models:
            try:
                resp = self.client.models.generate_content(model=model, contents=parts, config=config)
                self.count()
                return resp.parsed if resp.parsed is not None else schema.model_validate_json(resp.text)
            except errors.ClientError as exc:
                last = exc
                if getattr(exc, "code", None) == 429:
                    continue  # the next Gemini model has its own quota
            except Exception as exc:
                last = exc
        if last is not None and getattr(last, "code", None) == 429:
            raise RateLimited(f"Kuota Gemini habis: {last}", retry_after=60)
        raise ProviderError(f"Gemini gagal: {last}")


class OpenAICompatProvider(Provider):
    """Groq, OpenRouter, OpenAI, Together, Ollama: anything that speaks POST /chat/completions."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, vision_model: str = "",
                 audio_model: str = "", timeout: float = 30, transport: httpx.BaseTransport | None = None,
                 reasoning_effort: str = ""):
        super().__init__()
        self.name = name
        self.model, self.vision_model, self.audio_model = model, vision_model, audio_model
        self.reasoning_effort = reasoning_effort
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    @property
    def label(self) -> str:
        return f"{self.name} {self.model}"

    def _post(self, path: str, **kwargs) -> dict:
        r = self.http.post(path, **kwargs)
        if r.status_code == 429:
            retry = r.headers.get("retry-after")
            raise RateLimited(f"{self.name}: limit tercapai", retry_after=float(retry) if retry and retry.replace(".", "").isdigit() else 60)
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _transcribe(self, media_path: str) -> str:
        if not self.audio_model:
            raise Unsupported(f"{self.name}: model transkripsi suara belum diatur")
        data = Path(media_path).read_bytes()
        mime = mimetypes.guess_type(media_path)[0] or "audio/ogg"
        out = self._post("/audio/transcriptions", data={"model": self.audio_model, "language": "id"},
                         files={"file": (Path(media_path).name, data, mime)})
        self.count()
        return out.get("text", "")

    def generate(self, system, text, schema, media_path=None, kind="text"):
        model = self.model
        user_content: str | list = text
        if media_path and kind == "audio":
            user_content = f"{text}\n<transkrip_pesan_suara>{self._transcribe(media_path)}</transkrip_pesan_suara>"
        elif media_path and kind == "image":
            if not self.vision_model:
                raise Unsupported(f"{self.name}: model gambar belum diatur")
            model = self.vision_model
            mime = mimetypes.guess_type(media_path)[0] or "image/png"
            b64 = base64.b64encode(Path(media_path).read_bytes()).decode()
            user_content = [{"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
        messages = [
            {"role": "system", "content": f"{system}\n\nBalas HANYA dengan satu objek JSON berbentuk:\n{compact_shape(schema)}"},
            {"role": "user", "content": user_content},
        ]
        body = {"model": model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        for attempt in range(2):
            out = self._post("/chat/completions", json=body)
            self.count()
            content = out["choices"][0]["message"]["content"] or ""
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                if attempt == 1:
                    raise ProviderError(f"{self.name}: JSON tidak sesuai skema: {exc.errors()[:2]}") from exc
                # One repair round: show the model its own mistake.
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"JSON itu tidak valid: {exc.errors()[:3]}. Kirim ulang JSON yang benar saja."}]
        raise ProviderError("unreachable")
