"""Offline checks for llm.py, every provider (no network): python -m unittest discover tests"""
import json
import os
import unittest
from unittest import mock

import requests

import llm
from llm import LLMUnavailable

SECRET = "sk-test-not-a-real-key"


def fake_response(status, body):
    r = requests.Response()
    r.status_code = status
    r.headers["Content-Type"] = "application/json"
    r._content = (body if isinstance(body, str) else json.dumps(body)).encode()
    return r


def anthropic_reply(text):
    return fake_response(200, {"content": [{"type": "text", "text": text}]})


def openai_reply(text):
    return fake_response(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


@mock.patch.object(llm, "load_dotenv", lambda: None)  # never read the real .env
@mock.patch.object(llm.time, "sleep")                  # skip real backoff waits
class TestProviders(unittest.TestCase):
    def call(self, env, responses):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm.requests, "post", side_effect=responses) as post:
            result = llm.complete_json("sys", "prompt")
        return result, post

    def test_anthropic(self, sleep):
        r, post = self.call({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "m", "LLM_API_KEY": SECRET},
                            [anthropic_reply('Here: {"ok": true}')])
        self.assertEqual(r, {"ok": True})
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(kw["headers"]["x-api-key"], SECRET)
        self.assertEqual(kw["json"]["system"], "sys")
        self.assertEqual(kw["json"]["model"], "m")

    def test_anthropic_legacy_names(self, sleep):
        r, post = self.call({"ANTHROPIC_MODEL": "old-m", "ANTHROPIC_API_KEY": SECRET}, [anthropic_reply('{"ok": 1}')])
        self.assertEqual(r, {"ok": 1})
        self.assertEqual(post.call_args.kwargs["json"]["model"], "old-m")

    def test_openai_style_providers(self, sleep):
        cases = {
            "openai": ({"LLM_API_KEY": SECRET}, "https://api.openai.com/v1/chat/completions"),
            "gemini": ({"LLM_API_KEY": SECRET}, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
            "xai": ({"LLM_API_KEY": SECRET}, "https://api.x.ai/v1/chat/completions"),
            "ollama": ({}, "http://localhost:11434/v1/chat/completions"),
            "openai_compatible": ({"LLM_API_KEY": SECRET, "LLM_BASE_URL": "https://example.test/v1/"},
                                  "https://example.test/v1/chat/completions"),
        }
        for provider, (extra, url) in cases.items():
            with self.subTest(provider=provider):
                r, post = self.call({"LLM_PROVIDER": provider, "LLM_MODEL": "m", **extra},
                                    [openai_reply('```json\n{"ok": true}\n```')])
                self.assertEqual(r, {"ok": True})
                self.assertEqual(post.call_args.args[0], url)
                body, headers = post.call_args.kwargs["json"], post.call_args.kwargs["headers"]
                self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
                self.assertEqual(body["model"], "m")
                self.assertEqual("authorization" in headers, bool(extra.get("LLM_API_KEY")))

    def test_anthropic_key_not_used_for_other_providers(self, sleep):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "ANTHROPIC_API_KEY": SECRET}, clear=True):
            self.assertFalse(llm.available())

    def test_reasoning_model_format_retry(self, sleep):
        r, post = self.call({"LLM_PROVIDER": "openai", "LLM_MODEL": "o4", "LLM_API_KEY": SECRET},
                            [fake_response(400, "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."),
                             openai_reply('{"ok": true}')])
        self.assertEqual(r, {"ok": True})
        body = post.call_args.kwargs["json"]
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_retries_then_gives_up(self, sleep):
        with self.assertRaises(LLMUnavailable):
            self.call({"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "LLM_API_KEY": SECRET}, [fake_response(503, "{}")] * 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])

    def test_connection_error(self, sleep):
        with self.assertRaises(LLMUnavailable):
            self.call({"LLM_PROVIDER": "ollama", "LLM_MODEL": "m"}, [requests.ConnectionError()] * 4)

    def test_not_configured(self, sleep):
        for env in ({}, {"LLM_PROVIDER": "openai", "LLM_MODEL": "m"}, {"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "m"},
                    {"LLM_PROVIDER": "nonsense", "LLM_MODEL": "m", "LLM_API_KEY": SECRET}):
            with self.subTest(env=env), self.assertRaises(LLMUnavailable):
                self.call(env, [])

    def test_bad_json_reply(self, sleep):
        with self.assertRaises(LLMUnavailable):
            self.call({"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "LLM_API_KEY": SECRET}, [openai_reply("no json here")])

    def test_errors_never_contain_the_key(self, sleep):
        for responses in ([fake_response(401, '{"error": "invalid key"}')], [openai_reply("not json")]):
            with self.subTest(), self.assertRaises(LLMUnavailable) as cm:
                self.call({"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "LLM_API_KEY": SECRET}, responses)
            self.assertNotIn(SECRET, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
