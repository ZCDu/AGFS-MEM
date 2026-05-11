import json
import logging
import httpx

logger = logging.getLogger(__name__)


class LLMClient:
    """OpenAI-compatible LLM HTTP client."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self._url = base_url.rstrip("/")
        self._key = api_key
        self._model = model

    async def generate_json(self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 2000) -> str:
        """Call LLM with json_object response format, return raw content string."""
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
            logger.debug(f"LLM response: {content[:200]}...")
            return content

    async def extract_json_field(self, system: str, user: str, field: str = "facts") -> list[str]:
        """Call LLM, parse JSON, return values for a specific field."""
        try:
            content = await self.generate_json(system, user)
            parsed = json.loads(content)
            return parsed.get(field, [])
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}")
            return []
