import json
import logging
from typing import Protocol
import httpx

logger = logging.getLogger(__name__)


class ChatModelProvider(Protocol):
    """Provider interface for structured chat model calls."""

    async def generate_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.1,
        max_tokens: int = 2000,
    ) -> tuple[str, dict]:
        ...


class OpenAICompatibleProvider:
    """OpenAI-compatible chat completions provider."""

    DEFAULT_MODEL = "qwen-plus"

    def __init__(self, base_url: str, api_key: str, model: str):
        self._url = base_url.rstrip("/")
        self._key = api_key
        self._model = model or self.DEFAULT_MODEL

    async def generate_json(
        self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 2000,
    ) -> tuple[str, dict]:
        """Call LLM with json_object response format.

        Returns (content, usage_dict) where usage_dict has keys:
          prompt_tokens, completion_tokens, total_tokens.
        """
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
            resp = await client.post(f"{self._url}/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            logger.debug(f"LLM response: {content[:200]}..., usage={usage}")
            return content, usage


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek chat provider using its OpenAI-compatible API."""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, base_url: str, api_key: str, model: str):
        super().__init__(
            base_url=base_url or self.DEFAULT_BASE_URL,
            api_key=api_key,
            model=model or self.DEFAULT_MODEL,
        )


def create_chat_model_provider(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
) -> ChatModelProvider:
    provider_name = provider.lower()
    if provider_name in ("openai-compatible", "openai_compatible", "openai"):
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model)
    if provider_name == "deepseek":
        return DeepSeekProvider(base_url=base_url, api_key=api_key, model=model)
    raise ValueError(
        f"Unsupported LLM provider {provider!r}; expected 'openai-compatible' or 'deepseek'"
    )


class LLMClient:
    """Structured LLM client facade used by memory extraction."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        provider: str = "openai-compatible",
        chat_provider: ChatModelProvider | None = None,
    ):
        self._provider = chat_provider or create_chat_model_provider(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    async def generate_json(
        self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 2000,
    ) -> tuple[str, dict]:
        return await self._provider.generate_json(
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def extract_json_field(self, system: str, user: str, field: str = "facts") -> tuple[list[str], dict]:
        """Call LLM, parse JSON, return (values, usage_dict)."""
        try:
            content, usage = await self.generate_json(system, user)
            parsed = json.loads(content)
            return parsed.get(field, []), usage
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}")
            return [], {}
