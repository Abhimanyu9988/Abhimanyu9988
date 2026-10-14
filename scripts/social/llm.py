"""Thin clients for Claude, OpenAI and Gemini that return parsed JSON answers."""

import json
import re
import time

from common import HttpError, env, http, log

# USD per million tokens (input, output). Only used for the cost note on drafts;
# unknown models simply show no cost.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5.5": (5.0, 30.0),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
}

PROVIDERS = {
    "claude": {"label": "Claude", "key": "ANTHROPIC_API_KEY",
               "model_var": "CLAUDE_MODEL", "default": "claude-sonnet-5-5"},
    "openai": {"label": "ChatGPT", "key": "OPENAI_API_KEY",
               "model_var": "OPENAI_MODEL", "default": "gpt-5.6-terra"},
    "gemini": {"label": "Gemini", "key": "GEMINI_API_KEY",
               "model_var": "GEMINI_MODEL", "default": "gemini-3.8-flash"},
}

MAX_OUTPUT = 4000


class Usage:
    def __init__(self):
        self.cost = 0.0
        self.calls = 0
        self.unknown_price = False

    def add(self, model, tokens_in, tokens_out):
        self.calls += 1
        price = PRICES.get(model)
        if price is None:
            self.unknown_price = True
            return
        self.cost += tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]

    def note(self):
        if self.calls == 0:
            return ""
        if self.unknown_price:
            return f"{self.calls} model calls"
        return f"{self.calls} model calls, about ${self.cost:.3f}"


def available(name):
    return bool(env(PROVIDERS[name]["key"]))


def model_of(name):
    p = PROVIDERS[name]
    return env(p["model_var"], p["default"])


def _claude(system, user):
    model = model_of("claude")
    _, data, _ = http("POST", "https://api.anthropic.com/v1/messages", {
        "x-api-key": env("ANTHROPIC_API_KEY", required=True),
        "anthropic-version": "2023-06-01",
    }, {
        "model": model, "max_tokens": MAX_OUTPUT, "system": system,
        "messages": [{"role": "user", "content": user}],
    })
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    usage = data.get("usage", {})
    return text, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _openai(system, user):
    model = model_of("openai")
    _, data, _ = http("POST", "https://api.openai.com/v1/responses", {
        "Authorization": "Bearer " + env("OPENAI_API_KEY", required=True),
    }, {
        "model": model, "instructions": system, "input": user,
        "max_output_tokens": MAX_OUTPUT,
    })
    text = data.get("output_text") or ""
    if not text:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") in ("output_text", "text"):
                        text += part.get("text", "")
    usage = data.get("usage", {})
    return text, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _gemini(system, user):
    model = model_of("gemini")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    _, data, _ = http("POST", url, {
        "x-goog-api-key": env("GEMINI_API_KEY", required=True),
    }, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT * 2},
    })
    text = ""
    for cand in data.get("candidates", [])[:1]:
        for part in (cand.get("content") or {}).get("parts", []):
            if not part.get("thought"):
                text += part.get("text", "")
    usage = data.get("usageMetadata", {})
    return (text, model, usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0))


CALLERS = {"claude": _claude, "openai": _openai, "gemini": _gemini}


def extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def ask_json(name, system, user, usage, required_keys=()):
    """Ask one provider for a JSON object. Retries once on bad JSON or a 5xx."""
    last = None
    for attempt in range(3):
        try:
            text, model, tin, tout = CALLERS[name](system, user)
            usage.add(model, tin, tout)
            obj = extract_json(text)
            missing = [k for k in required_keys if k not in obj]
            if missing:
                raise ValueError(f"reply missing {missing}")
            return obj
        except HttpError as err:
            last = err
            if err.status in (429, 500, 502, 503, 504, 529) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"{PROVIDERS[name]['label']} API error {err.status}: "
                               f"{err.body[:300]}") from None
        except (ValueError, json.JSONDecodeError) as err:
            last = err
            log(f"{name}: unusable reply ({err}), retrying")
            user = user + "\n\nReturn only one valid JSON object, nothing else."
    raise RuntimeError(f"{PROVIDERS[name]['label']} gave no usable answer: {last}")


def selftest(name):
    usage = Usage()
    obj = ask_json(name, "You answer with JSON only.",
                   'Reply with exactly {"ok": true}', usage, ("ok",))
    return obj.get("ok") is True, model_of(name)
