from __future__ import annotations

import contextvars

import httpx


class LLMClient:
    def __init__(self, settings):
        self.provider = settings.llm_provider
        self.model_fast = settings.llm_model_fast
        self.model_smart = settings.llm_model_smart
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url
        self._usage_tracker = contextvars.ContextVar("llm_usage_tracker", default=None)


    def start_usage_tracking(self):
        tracker = {"by_model": {}}
        token = self._usage_tracker.set(tracker)
        return token, tracker

    def stop_usage_tracking(self, token) -> None:
        self._usage_tracker.reset(token)

    def _record_usage(
        self, model: str, input_tokens: int = 0, output_tokens: int = 0,
        cached_input_tokens: int = 0, provider_cost_usd: float = 0.0,
    ) -> None:
        tracker = self._usage_tracker.get()
        if tracker is None:
            return
        row = tracker["by_model"].setdefault(
            model, {
                "input_tokens": 0, "output_tokens": 0,
                "cached_input_tokens": 0, "provider_cost_usd": 0.0,
            }
        )
        row["input_tokens"] += int(input_tokens or 0)
        row["output_tokens"] += int(output_tokens or 0)
        row["cached_input_tokens"] += int(cached_input_tokens or 0)
        row["provider_cost_usd"] += float(provider_cost_usd or 0.0)

    def estimate_usage_cost_usd(self, tracker: dict) -> float | None:
        # Current public API-equivalent rates per million tokens.
        pricing = {
            "deepseek-v4": (0.14, 0.28, 0.0028),
            "glm-5.3": (1.40, 4.40, 0.26),
            "glm-5.2": (1.40, 4.40, 0.26),
        }
        actual = sum(
            float(usage.get("provider_cost_usd", 0.0) or 0.0)
            for usage in tracker.get("by_model", {}).values()
        )
        if actual > 0:
            return actual

        total = 0.0
        saw_known = False
        for model, usage in tracker.get("by_model", {}).items():
            rates = next((v for k, v in pricing.items() if k in model.lower()), None)
            if not rates:
                continue
            saw_known = True
            input_rate, output_rate, cached_rate = rates
            total += usage.get("input_tokens", 0) / 1_000_000 * input_rate
            total += usage.get("output_tokens", 0) / 1_000_000 * output_rate
            total += usage.get("cached_input_tokens", 0) / 1_000_000 * cached_rate
        return total if saw_known else None

    def _model_for(self, tier: str) -> str:
        return self.model_fast if tier == "fast" else self.model_smart

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1800,
        tier: str = "smart",
        reasoning_effort: str | None = None,
    ) -> str:
        model = self._model_for(tier)
        # DeepSeek V4 can spend substantial latency on hidden reasoning.
        # For routine summarization, translation and grounded Q&A we disable it.
        if reasoning_effort == "none" and "deepseek" in model.lower():
            return await self._openai_compatible(
                system, user, max_tokens, model, reasoning_effort="none"
            )
        if self.provider == "anthropic":
            return await self._anthropic(system, user, max_tokens, model)
        return await self._openai_compatible(
            system, user, max_tokens, model, reasoning_effort=reasoning_effort
        )

    async def _openai_compatible(
        self, system: str, user: str, max_tokens: int, model: str,
        reasoning_effort: str | None = None,
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
        if reasoning_effort == "none" and "deepseek" in model.lower():
            if self.base_url and "openrouter.ai" in self.base_url:
                payload["reasoning"] = {"enabled": False}
            else:
                payload["thinking"] = {"type": "disabled"}
            payload["reasoning_effort"] = "none"
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        self._record_usage(
            model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            provider_cost_usd=usage.get("cost", 0.0),
        )
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
        usage = getattr(message, "usage", None)
        if usage is not None:
            self._record_usage(
                model,
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0),
            )
        return "\n".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        ).strip()
