"""Provider calls and structured-output capability handling for Topic construction."""

from __future__ import annotations

import asyncio
import inspect
import json
import random
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.tool import (
    FunctionTool,
    ToolSet,
)

from .topic_build_contracts import (
    TopicBuildValidationError,
)


class TopicBuildProviderMixin:
    async def _call_llm(
        self,
        prompt: str,
        system_prompt: str,
        *,
        output_contract: str | None = None,
    ) -> str:
        if self.llm_provider is None:
            raise RuntimeError("Topic build requires an LLM Provider")
        capability_key = self._provider_capability_key(self.llm_provider)
        if (
            output_contract
            and self._structured_output_capabilities.get(capability_key) is not False
            and self._provider_accepts_tool_output()
        ):
            tool_name, tool_description, parameters = self._structured_output_spec(
                output_contract
            )
            tool = FunctionTool(
                name=tool_name,
                description=tool_description,
                parameters=parameters,
            )
            try:
                response = await self._request_llm(
                    prompt,
                    system_prompt,
                    func_tool=ToolSet([tool]),
                    tool_choice="required",
                )
                payload = self._tool_payload(response, tool_name)
                if payload is not None:
                    if (
                        self._structured_output_capabilities.get(capability_key)
                        is not True
                    ):
                        logger.info(
                            "[TopicMemory] LLM 工具结构化输出已启用 (%s)",
                            tool_name,
                        )
                    self._structured_output_capabilities[capability_key] = True
                    return json.dumps(payload, ensure_ascii=False)
                raw = str(getattr(response, "completion_text", "") or "").strip()
                if raw:
                    try:
                        self._parse_json_object(raw)
                    except TopicBuildValidationError:
                        logger.warning(
                            "[TopicMemory] Provider 本次忽略了必需的结构化输出工具"
                            "并返回非 JSON 文本，本次请求回退到文本模式"
                        )
                    else:
                        logger.info(
                            "[TopicMemory] Provider 本次忽略了结构化输出工具，"
                            "但返回了有效 JSON；仅接受本次文本结果"
                        )
                        return raw
                else:
                    logger.warning(
                        "[TopicMemory] Provider 本次未返回结构化输出工具参数或"
                        "JSON 文本，本次请求回退到文本模式"
                    )
            except Exception as exc:
                if not self._is_tool_output_unsupported(exc):
                    raise
                self._disable_structured_output(capability_key, str(exc))

        response = await self._request_llm(prompt, system_prompt)
        return str(response.completion_text)

    async def _request_llm(self, prompt: str, system_prompt: str, **kwargs: Any) -> Any:
        max_attempts = max(1, int(self.config.get("llm_max_retries", 3)))
        if self._provider_accepts_request_retry_budget():
            provider_kwargs = dict(kwargs)
            provider_kwargs.setdefault("request_max_retries", max_attempts)
            try:
                async with self._llm_semaphore:
                    return await self.llm_provider.text_chat(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **provider_kwargs,
                    )
            except Exception as exc:
                raise RuntimeError(
                    f"Topic LLM request failed after Provider retry budget "
                    f"({max_attempts} attempts): {exc}"
                ) from exc

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                async with self._llm_semaphore:
                    response = await self.llm_provider.text_chat(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                return response
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep((2**attempt) + random.uniform(0, 0.5))
        raise RuntimeError(f"Topic LLM request failed: {last_error}") from last_error

    def _provider_accepts_request_retry_budget(self) -> bool:
        """Use the Provider's retry loop when it exposes the AstrBot retry contract."""
        try:
            parameters = inspect.signature(self.llm_provider.text_chat).parameters
        except (TypeError, ValueError):
            return False
        return "request_max_retries" in parameters

    def _provider_accepts_tool_output(self) -> bool:
        try:
            parameters = inspect.signature(self.llm_provider.text_chat).parameters
        except (TypeError, ValueError):
            return True
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return True
        return "func_tool" in parameters and "tool_choice" in parameters

    @staticmethod
    def _tool_payload(response: Any, tool_name: str) -> dict[str, Any] | None:
        names = list(getattr(response, "tools_call_name", None) or [])
        arguments = list(getattr(response, "tools_call_args", None) or [])
        matches = [
            value
            for name, value in zip(names, arguments, strict=False)
            if str(name) == tool_name and isinstance(value, dict)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _is_tool_output_unsupported(exc: Exception) -> bool:
        message = str(exc).casefold()
        markers = (
            "tool call is not supported",
            "function calling is not supported",
            "function_calling is not supported",
            "tool use is not supported",
            "does not support tool",
            "unsupported parameter: tools",
            "unknown field tools",
            "unknown field: tools",
            "invalid tools",
            "invalid function parameters",
            "tool schema",
            "function schema",
            "tools[0]",
            "tool_choice",
            "func_tool",
        )
        return isinstance(exc, TypeError) or any(
            marker in message for marker in markers
        )

    def _disable_structured_output(
        self,
        capability_key: tuple[str, str, int],
        reason: str,
    ) -> None:
        if self._structured_output_capabilities.get(capability_key) is not False:
            logger.warning(
                "[TopicMemory] 当前 LLM Provider 无法可靠使用工具结构化输出，"
                "本次运行回退到 JSON 文本模式: %s",
                str(reason)[:500],
            )
        self._structured_output_capabilities[capability_key] = False

    async def _get_embeddings(self, texts: list[str]) -> list[list[float]]:
        provider = self.embedding_provider
        get_embeddings = getattr(provider, "get_embeddings", None)
        if callable(get_embeddings):
            return await get_embeddings(texts)
        get_batch = getattr(provider, "get_embeddings_batch", None)
        if callable(get_batch):
            try:
                return await get_batch(texts, batch_size=len(texts), tasks_limit=1)
            except TypeError:
                return await get_batch(texts)
        return [await provider.get_embedding(text) for text in texts]

    @staticmethod
    def _provider_identity(provider: Any) -> tuple[str, str]:
        if provider is None:
            return "", ""
        config = getattr(provider, "provider_config", {}) or {}
        if not isinstance(config, dict):
            config = {}
        return (
            str(config.get("id") or type(provider).__name__),
            str(config.get("model") or config.get("model_name") or ""),
        )

    @classmethod
    def _provider_capability_key(cls, provider: Any) -> tuple[str, str, int]:
        provider_id, model_id = cls._provider_identity(provider)
        return provider_id, model_id, id(provider)


__all__ = ["TopicBuildProviderMixin"]
