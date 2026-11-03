"""One small LLM wrapper for any provider named in .env. Every caller must have a deterministic
fallback: any failure (nothing configured, API error, reply not JSON) raises LLMUnavailable.

    LLM_PROVIDER   anthropic | openai | gemini | ollama | openai_compatible   (default anthropic)
    LLM_MODEL      model name, e.g. claude-sonnet-4-6, gpt-4.1, gemini-2.5-flash, llama3.1
    LLM_API_KEY    not needed for ollama (or a keyless openai_compatible server)
    LLM_BASE_URL   required for openai_compatible; optional override for the others

ANTHROPIC_API_KEY / ANTHROPIC_MODEL still work for the anthropic provider.
Two wire formats cover them all: the Anthropic Messages API and OpenAI-style chat completions.

    python llm.py --test     # one tiny request: prints provider, model and OK or the error
"""
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

MAX_RETRIES = 3
TIMEOUT = 120
RETRY_STATUS = {429, 500, 502, 503, 504, 529}

# provider -> (wire format, default base URL)
PROVIDERS = {
    "anthropic": ("anthropic", "https://api.anthropic.com/v1"),
    "openai": ("openai", "https://api.openai.com/v1"),
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "ollama": ("openai", "http://localhost:11434/v1"),
    "openai_compatible": ("openai", None),
}
KEYLESS = {"ollama", "openai_compatible"}


class LLMUnavailable(Exception):
    """Nothing configured, API error, or a reply that is not the JSON asked for."""


def settings():
    load_dotenv()
    provider = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    legacy = provider == "anthropic"
    return {"provider": provider,
            "model": os.getenv("LLM_MODEL") or (os.getenv("ANTHROPIC_MODEL") if legacy else "") or "",
            "key": os.getenv("LLM_API_KEY") or (os.getenv("ANTHROPIC_API_KEY") if legacy else "") or "",
            "base": (os.getenv("LLM_BASE_URL") or PROVIDERS.get(provider, (None, None))[1] or "").rstrip("/")}


def problem(s=None):
    """Why the configured LLM cannot be used, or '' when it can."""
    s = s or settings()
    if s["provider"] not in PROVIDERS:
        return f"unknown LLM_PROVIDER {s['provider']!r} (use one of: {', '.join(PROVIDERS)})"
    if not s["model"]:
        return "LLM_MODEL is blank"
    if not s["base"]:
        return f"LLM_BASE_URL is required for {s['provider']}"
    if not s["key"] and s["provider"] not in KEYLESS:
        return f"LLM_API_KEY is blank for {s['provider']}"
    return ""


def available():
    return not problem()


def model():
    return settings()["model"]


def describe():
    """'provider model', recorded on every LLM result."""
    s = settings()
    return f"{s['provider']} {s['model']}"


def _post(url, headers, body):
    """POST with max 3 retries on rate limits / server errors. Returns the parsed JSON body."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == MAX_RETRIES:
                raise LLMUnavailable(f"cannot reach {url} ({type(e).__name__})") from None
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            raise LLMUnavailable(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise LLMUnavailable(f"non-JSON response from {url}") from None


def _anthropic(s, system, prompt, max_tokens):
    data = _post(f"{s['base']}/messages",
                 {"x-api-key": s["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                 {"model": s["model"], "max_tokens": max_tokens, "temperature": 0, "system": system,
                  "messages": [{"role": "user", "content": prompt}]})
    return "".join(b.get("text", "") for b in data.get("content", []))


def _openai(s, system, prompt, max_tokens):
    headers = {"content-type": "application/json"}
    if s["key"]:
        headers["authorization"] = f"Bearer {s['key']}"
    body = {"model": s["model"], "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    try:
        data = _post(f"{s['base']}/chat/completions", headers, body)
    except LLMUnavailable as e:
        # Newer OpenAI reasoning models reject max_tokens and temperature; retry once in their format.
        if "HTTP 400" not in str(e) or not ("max_tokens" in str(e) or "temperature" in str(e)):
            raise
        body = {k: v for k, v in body.items() if k not in ("max_tokens", "temperature")}
        body["max_completion_tokens"] = max_tokens
        data = _post(f"{s['base']}/chat/completions", headers, body)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMUnavailable("unexpected chat completions response shape") from None


def complete(system, prompt, max_tokens=2000):
    """Return the model's text reply (temperature 0 where the model allows it)."""
    s = settings()
    why = problem(s)
    if why:
        raise LLMUnavailable(f"no usable LLM configured: {why}")
    send = _anthropic if PROVIDERS[s["provider"]][0] == "anthropic" else _openai
    return send(s, system, prompt, max_tokens)


def complete_json(system, prompt, max_tokens=2000):
    """Return the reply parsed as a JSON object."""
    text = complete(system, prompt, max_tokens)
    start, end = text.find("{"), text.rfind("}")
    try:
        result = json.loads(text[start:end + 1])
    except ValueError:
        raise LLMUnavailable(f"LLM reply was not JSON: {text[:120]!r}") from None
    if not isinstance(result, dict):
        raise LLMUnavailable("LLM reply was not a JSON object")
    return result


if __name__ == "__main__":
    if sys.argv[1:] != ["--test"]:
        sys.exit("usage: python llm.py --test")
    s = settings()
    print(f"provider: {s['provider']}   model: {s['model'] or '-'}   base: {s['base'] or '-'}   "
          f"key: {'set' if s['key'] else 'not set'}")
    try:
        reply = complete_json("Reply with JSON only.", 'Return exactly {"ok": true}', max_tokens=50)
        ok = reply.get("ok") is True
        print("RESULT:", "OK" if ok else f"unexpected reply {reply}")
        sys.exit(0 if ok else 1)
    except LLMUnavailable as e:
        print(f"RESULT: LLM unavailable: {e}")
        sys.exit(1)
