"""Provider chain: OpenAI-compatible adapter, rate-limit cool-down, fallbacks, and masking. No network needed."""

import json
from datetime import date

import httpx

from app.llm.client import LLMService
from app.llm.providers import OpenAICompatProvider, Provider, RateLimited
from app.llm.schemas import Understanding

ORDER_JSON = {"intent": "order_baru", "lines": [{"raw": "indomie goreng", "qty": 2, "unit": "dus"}]}
CTX = {"today": date(2026, 11, 9), "weekday": "Senin", "pii_names": ["Ratna"]}


def compat(handler, **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider("tes", "https://llm.test/v1", "kunci", "model-tes", transport=httpx.MockTransport(handler), **kw)


def chat_reply(payload) -> httpx.Response:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_compat_provider_sends_schema_and_parses_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return chat_reply(ORDER_JSON)

    u = compat(handler).generate("sistem", "indomie goreng 2 dus", Understanding)
    assert u.intent == "order_baru" and u.lines[0].qty == 2
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["model"] == "model-tes"
    assert "intent:" in seen["body"]["messages"][0]["content"] and "$defs" not in seen["body"]["messages"][0]["content"]
    assert seen["auth"] == "Bearer kunci"


def test_compat_provider_repairs_invalid_json_once():
    replies = iter([chat_reply({"intent": "bukan_niat"}), chat_reply(ORDER_JSON)])
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return next(replies)

    u = compat(handler).generate("sistem", "x", Understanding)
    assert u.intent == "order_baru"
    assert len(calls) == 2 and "tidak valid" in calls[1]["messages"][-1]["content"]


class Fake(Provider):
    def __init__(self, name, result=None, error=None):
        super().__init__()
        self.name, self.result, self.error, self.hits = name, result, error, 0

    def generate(self, system, text, schema, media_path=None, kind="text"):
        self.hits += 1
        self.last_text = text
        if self.error:
            raise self.error
        self.count()
        return schema.model_validate(self.result)


def test_rate_limited_provider_is_skipped_until_cool_down_ends():
    first = Fake("pertama", error=RateLimited("limit", retry_after=120))
    second = Fake("kedua", result=ORDER_JSON)
    svc = LLMService(providers=[first, second])
    u, label = svc.understand("indomie goreng 2 dus", ctx=CTX)
    assert u.intent == "order_baru" and label == "kedua"
    svc.understand("indomie goreng 2 dus", ctx=CTX)
    assert first.hits == 1, "a cooling provider must not be called again"
    assert second.hits == 2
    assert svc.status()[0]["cooling_seconds"] > 0


def test_all_providers_failing_falls_back_to_offline_parser():
    svc = LLMService(providers=[Fake("a", error=RuntimeError("putus")), Fake("b", error=RuntimeError("putus"))])
    u, source = svc.understand("indomie goreng 2 dus", ctx=CTX)
    assert source == "heuristic" and u.lines[0].raw == "indomie goreng"


def test_image_without_vision_model_moves_to_next_provider(tmp_path):
    img = tmp_path / "nota.png"
    img.write_bytes(b"\x89PNG\r\n")
    no_vision = compat(lambda r: chat_reply(ORDER_JSON))
    backup = Fake("cadangan", result=ORDER_JSON)
    svc = LLMService(providers=[no_vision, backup])
    _, label = svc.understand("", kind="image", media_path=str(img), ctx=CTX)
    assert label == "cadangan"


def test_personal_data_is_masked_before_it_reaches_the_provider():
    p = Fake("a", result=ORDER_JSON)
    LLMService(providers=[p]).understand("Bu Ratna, hubungi 081234567890. indomie goreng 2 dus", ctx=CTX)
    assert "081234567890" not in p.last_text and "Ratna" not in p.last_text
    assert "indomie goreng 2 dus" in p.last_text


def test_calendar_maths_comes_from_code_not_the_llm():
    from app.llm.heuristic import days_from_phrase

    saturday = date(2026, 11, 7)
    assert days_from_phrase("jumat", saturday) == 6
    assert days_from_phrase("kirim kamis depan", date(2026, 11, 9)) == 10
    assert days_from_phrase("tgl 12", saturday) == 5
    assert days_from_phrase("secepatnya", saturday) is None


def test_heuristic_choice_has_no_providers():
    svc = LLMService("heuristic")
    assert svc.mode == "heuristic" and svc.status() == []
