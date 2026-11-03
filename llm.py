"""One small LLM wrapper. Provider and model come from .env (LLM_PROVIDER, ANTHROPIC_API_KEY,
ANTHROPIC_MODEL). Every caller must have a deterministic fallback: any failure raises
LLMUnavailable and the caller uses its rule-based path instead.
"""
import json
import os
import time

import requests
from dotenv import load_dotenv

API_URL = "https://api.anthropic.com/v1/messages"
MAX_RETRIES = 3
TIMEOUT = 90
RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class LLMUnavailable(Exception):
    """No key configured, API error, or a reply that is not the JSON asked for."""


def model():
    load_dotenv()
    return os.getenv("ANTHROPIC_MODEL") or ""


def available():
    load_dotenv()
    return (os.getenv("LLM_PROVIDER") or "anthropic") == "anthropic" and bool(os.getenv("ANTHROPIC_API_KEY") and model())


def complete(system, prompt, max_tokens=2000):
    """Return the model's text reply (temperature 0)."""
    if not available():
        raise LLMUnavailable("no LLM configured (ANTHROPIC_API_KEY / ANTHROPIC_MODEL blank)")
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model(), "max_tokens": max_tokens, "temperature": 0, "system": system,
            "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == MAX_RETRIES:
                raise LLMUnavailable(f"cannot reach the LLM API ({type(e).__name__})") from None
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            raise LLMUnavailable(f"LLM API HTTP {resp.status_code}: {resp.text[:200]}")
        return "".join(b.get("text", "") for b in resp.json().get("content", []))


def complete_json(system, prompt, max_tokens=2000):
    """Return the reply parsed as a JSON object."""
    text = complete(system, prompt, max_tokens)
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise LLMUnavailable(f"LLM reply was not JSON: {text[:120]!r}") from None
