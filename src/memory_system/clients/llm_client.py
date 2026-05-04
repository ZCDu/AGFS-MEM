import json
import httpx
from memory_system.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self._url = settings.llm_api_url.rstrip("/")
        self._key = settings.llm_api_key.get_secret_value()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> str:
        """Send chat completion request and return text response."""
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": "default",
                    "messages": full_messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"LLM API error: status={resp.status_code}, body={resp.text}"
                )
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_json(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
    ) -> dict | list:
        """Send chat request and parse response as JSON."""
        text = await self.chat(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        # Try to extract JSON from the response text
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code fences
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Failed to parse LLM JSON response: {text[:200]}")
