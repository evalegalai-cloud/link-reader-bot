from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, settings):
        self.provider = settings.llm_provider
        self.model_fast = settings.llm_model_fast
        self.model_smart = settings.llm_model_smart
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url

    def _model_for(self, tier: str) -> str:
        return self.model_fast if tier == "fast" else self.model_smart

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1800,
        tier: str = "smart",
    ) -> str:
        model = self._model_for(tier)
        if self.provider == "anthropic":
            return await self._anthropic(system, user, max_tokens, model)
        return await self._openai_compatible(system, user, max_tokens, model)

    async def _openai_compatible(
        self, system: str, user: str, max_tokens: int, model: str
    ) -> str:
        if not self.base_url:
            raise RuntimeError("LLM_BASE_URL is required for openai_compatible provider")
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            if not url.endswith("/v1"):
                url += "/v1"
            url += "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            return "\n".join(x.get("text", "") for x in content if isinstance(x, dict)).strip()
        return str(content).strip()

    async def _anthropic(
        self, system: str, user: str, max_tokens: int, model: str
    ) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self.api_key, base_url=self.base_url)
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.2,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "\n".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        ).strip()
