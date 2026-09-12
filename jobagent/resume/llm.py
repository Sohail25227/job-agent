"""LLM access with free-first provider selection.

Providers from the config are probed in order and the first usable one wins, so
a local Ollama install is preferred over a hosted free tier, which is preferred
over a paid key. If nothing is usable every caller has a non-LLM fallback - the
pipeline never hard-depends on one.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from ..config import Config, LLMProvider

log = logging.getLogger(__name__)

JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class LLMUnavailable(RuntimeError):
    pass


def _is_local(provider: LLMProvider) -> bool:
    base = provider.base_url or ""
    return "localhost" in base or "127.0.0.1" in base


def _local_is_up(provider: LLMProvider) -> bool:
    try:
        response = httpx.get(f"{(provider.base_url or '').rstrip('/')}/models", timeout=2.0)
        return response.status_code < 500
    except Exception:
        return False


def select_provider(config: Config) -> LLMProvider | None:
    if not config.llm.enabled:
        return None
    for provider in config.llm.providers:
        if _is_local(provider):
            if _local_is_up(provider):
                return provider
            log.debug("local provider %s not reachable, trying next", provider.name)
            continue
        if provider.api_key:
            return provider
        log.debug("provider %s has no key in %s, trying next", provider.name, provider.api_key_env)
    return None


class LLMClient:
    def __init__(self, config: Config, provider: LLMProvider):
        from openai import OpenAI  # imported lazily so the package stays optional

        self.config = config
        self.provider = provider
        self._client = OpenAI(
            api_key=provider.api_key or "local",
            base_url=provider.base_url,
            timeout=config.llm.timeout_seconds,
            max_retries=2,
        )

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.provider.model}"

    def complete_json(self, system: str, user: str) -> dict:
        """Ask for a JSON object and parse it defensively."""
        kwargs = {
            "model": self.provider.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.llm.temperature,
            "max_tokens": self.config.llm.max_tokens,
        }
        try:
            response = self._client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except Exception as exc:
            # Not every free endpoint supports response_format; retry plainly.
            log.debug("json mode rejected by %s (%s), retrying without it", self.label, exc)
            response = self._client.chat.completions.create(**kwargs)

        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise LLMUnavailable(f"{self.label} returned an empty response")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = JSON_BLOCK.search(content)
            if not match:
                raise LLMUnavailable(f"{self.label} did not return JSON: {content[:200]}")
            return json.loads(match.group(0))


def get_llm(config: Config) -> LLMClient | None:
    provider = select_provider(config)
    if provider is None:
        return None
    try:
        return LLMClient(config, provider)
    except Exception as exc:
        log.warning("could not initialise LLM %s: %s", provider.name, exc)
        return None
