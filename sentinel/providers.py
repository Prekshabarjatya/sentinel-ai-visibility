"""LLM providers. Each returns (text, cited_urls). 30 s timeout, one retry.

Model names come from environment variables so they can be updated without code changes.
Verify each vendor's current endpoint, model name and terms before production use.
"""
import hashlib
import os
import time

import requests

TIMEOUT = 30


class ProviderError(Exception):
    pass


def _post(url, headers, payload):
    last = None
    for attempt in range(2):  # one retry
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise ProviderError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ProviderError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise ProviderError(str(last))


class Provider:
    name = "base"

    def ask(self, prompt: str, region: str = "us", language: str = "en") -> tuple[str, list[str]]:
        raise NotImplementedError

    @staticmethod
    def framed(prompt, region, language):
        return f"{prompt}\n\n(Answer for a user in region '{region}', language '{language}'. Cite sources.)"


class OpenAIProvider(Provider):
    name = "openai"

    def ask(self, prompt, region="us", language="en"):
        key = os.environ["OPENAI_API_KEY"]
        data = _post(
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {key}"},
            {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
             "messages": [{"role": "user", "content": self.framed(prompt, region, language)}]},
        )
        return data["choices"][0]["message"]["content"], []


class AnthropicProvider(Provider):
    name = "anthropic"

    def ask(self, prompt, region="us", language="en"):
        key = os.environ["ANTHROPIC_API_KEY"]
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 1024,
             "messages": [{"role": "user", "content": self.framed(prompt, region, language)}]},
        )
        return "".join(b.get("text", "") for b in data["content"]), []


class GeminiProvider(Provider):
    name = "gemini"

    def ask(self, prompt, region="us", language="en"):
        key = os.environ["GEMINI_API_KEY"]
        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        data = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            {},
            {"contents": [{"parts": [{"text": self.framed(prompt, region, language)}]}]},
        )
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts), []


class PerplexityProvider(Provider):
    name = "perplexity"

    def ask(self, prompt, region="us", language="en"):
        key = os.environ["PERPLEXITY_API_KEY"]
        data = _post(
            "https://api.perplexity.ai/chat/completions",
            {"Authorization": f"Bearer {key}"},
            {"model": os.environ.get("PERPLEXITY_MODEL", "sonar"),
             "messages": [{"role": "user", "content": self.framed(prompt, region, language)}]},
        )
        return data["choices"][0]["message"]["content"], data.get("citations", [])


class OpenAICompatProvider(Provider):
    """Free-tier engines with an OpenAI-style API (Groq, OpenRouter)."""

    def __init__(self, name, url, key_env, model_env, default_model):
        self.name, self.url, self.key_env, self.model_env, self.default_model = name, url, key_env, model_env, default_model

    def ask(self, prompt, region="us", language="en"):
        data = _post(
            self.url,
            {"Authorization": f"Bearer {os.environ[self.key_env]}"},
            {"model": os.environ.get(self.model_env, self.default_model),
             "messages": [{"role": "user", "content": self.framed(prompt, region, language)}]},
        )
        return data["choices"][0]["message"]["content"], []


class MockProvider(Provider):
    """Deterministic fake engine for testing the pipeline with no API keys or cost."""

    def __init__(self, name="mock", client_rate=0.4, client=None):
        self.name = name
        self.rate = client_rate
        self.client = client or {}

    def ask(self, prompt, region="us", language="en"):
        h = int(hashlib.sha256(f"{self.name}|{prompt}|{time.time_ns()}".encode()).hexdigest(), 16)
        roll = (h % 1000) / 1000
        brand = self.client.get("brand", "Acme")
        domain = self.client.get("domain", "acme.com")
        comps = self.client.get("competitors", [{"brand": "Rival", "domain": "rival.com"}])
        comp = comps[h % len(comps)]
        if roll < self.rate:
            return (f"For this, {brand} is a leading, recommended option. See https://{domain}/product. "
                    f"{comp['brand']} is also available.", [f"https://{domain}/product"])
        return (f"{comp['brand']} is a popular choice. Details at https://{comp['domain']}/features.",
                [f"https://{comp['domain']}/features"])


REAL = {p.name: p for p in (OpenAIProvider(), AnthropicProvider(), GeminiProvider(), PerplexityProvider(),
    OpenAICompatProvider("groq", "https://api.groq.com/openai/v1/chat/completions",
                         "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    OpenAICompatProvider("openrouter", "https://openrouter.ai/api/v1/chat/completions",
                         "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "openrouter/free"))}


def build(names, mock=False, client=None):
    if mock:
        return [MockProvider(n, client_rate=0.25 + 0.15 * i, client=client) for i, n in enumerate(names)]
    return [REAL[n] for n in names]
