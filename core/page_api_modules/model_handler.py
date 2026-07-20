"""Model inspection and connection-test endpoints for the plugin dashboard."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

if TYPE_CHECKING:
    from ..plugin_initializer import PluginInitializer
    from .utils import PageApiUtils


class ModelHandler:
    """Expose only the providers actually selected by LivingMemory."""

    _ROLES = ("llm", "embedding", "rerank")

    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    async def list_models(
        self, initializer: "PluginInitializer | None"
    ) -> dict[str, Any]:
        if initializer is None:
            return self.utils.error("插件初始化器尚未创建")
        try:
            return self.utils.ok(
                {
                    "models": [
                        self._model_payload(initializer, role) for role in self._ROLES
                    ]
                }
            )
        except Exception as exc:
            logger.error("[PageAPI] 获取模型信息失败", exc_info=True)
            return self.utils.error(self._safe_error(initializer, exc))

    async def test_connection(
        self, initializer: "PluginInitializer | None"
    ) -> dict[str, Any]:
        if initializer is None:
            return self.utils.error("插件初始化器尚未创建")
        payload = await request.get_json(silent=True) or {}
        role = str(payload.get("role") or "").strip().lower()
        if role not in self._ROLES:
            return self.utils.error("不支持的模型角色")
        provider = getattr(initializer, f"{role}_provider", None)
        if provider is None:
            return self.utils.error("该模型当前不可用，无法测试连接")

        started = time.perf_counter()
        try:
            details = await asyncio.wait_for(
                self._run_test(role, provider), timeout=60.0
            )
        except asyncio.TimeoutError:
            return self.utils.error("连接测试超时（60 秒）")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[PageAPI] %s 模型连接测试失败: %s",
                role,
                type(exc).__name__,
            )
            return self.utils.error(self._safe_error(initializer, exc))

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return self.utils.ok(
            {
                "role": role,
                "success": True,
                "latency_ms": elapsed_ms,
                "details": details,
            }
        )

    def _model_payload(
        self, initializer: "PluginInitializer", role: str
    ) -> dict[str, Any]:
        provider = getattr(initializer, f"{role}_provider", None)
        manager = initializer.config_manager
        configured_id = str(
            manager.get(f"provider_settings.{role}_provider_id", "") or ""
        ).strip()
        cloudflare_enabled = bool(manager.get("cloudflare_rerank.enabled", False))
        config = self._provider_config(provider)
        provider_id = str(config.get("id") or "").strip()

        if role == "rerank" and cloudflare_enabled:
            if provider_id == "cloudflare_workers_ai_rerank":
                selection = "cloudflare"
            elif provider is not None:
                selection = "fallback"
            else:
                selection = "unavailable"
        elif configured_id and provider is not None and provider_id == configured_id:
            selection = "explicit"
        elif configured_id and provider is not None:
            selection = "fallback"
        elif role in {"llm", "embedding"} and provider is not None:
            selection = "astrbot_default"
        elif role == "rerank" and provider is not None:
            selection = "explicit"
        elif role == "rerank":
            selection = "unavailable" if configured_id else "vector_only"
        else:
            selection = "unavailable"

        model = self._model_name(provider, config)
        provider_type = str(config.get("type") or "").strip()
        if not provider_type and provider is not None:
            provider_type = type(provider).__name__

        extra: dict[str, Any] = {}
        if role == "embedding" and provider is not None:
            get_dim = getattr(provider, "get_dim", None)
            if callable(get_dim):
                try:
                    dimension = int(get_dim())
                    if dimension > 0:
                        extra["dimension"] = dimension
                except (TypeError, ValueError, RuntimeError):
                    pass
        if role == "rerank" and cloudflare_enabled:
            cloudflare_model = str(
                manager.get("cloudflare_rerank.model", "") or ""
            ).strip()
            model = model or cloudflare_model
            extra["base_url"] = str(
                getattr(provider, "base_url", "")
                or manager.get("cloudflare_rerank.base_url", "")
                or ""
            )
            extra["score_mapping"] = (
                "sigmoid"
                if bool(
                    getattr(
                        provider,
                        "apply_sigmoid",
                        manager.get("cloudflare_rerank.apply_sigmoid", True),
                    )
                )
                else "raw"
            )
            extra["account_configured"] = bool(
                manager.get("cloudflare_rerank.account_id", "")
            )
            extra["credential_source"] = (
                "configuration"
                if manager.get("cloudflare_rerank.api_token", "")
                else "environment"
                if os.environ.get("CLOUDFLARE_AUTH_TOKEN")
                else "missing"
            )
            if configured_id:
                extra["fallback_provider_id"] = configured_id
            initialization_error = str(
                getattr(initializer, "rerank_initialization_error", "") or ""
            ).strip()
            if initialization_error:
                extra["initialization_error"] = initialization_error

        return {
            "role": role,
            "available": provider is not None,
            "testable": provider is not None,
            "selection": selection,
            "configured_provider_id": (
                "cloudflare_workers_ai_rerank"
                if role == "rerank" and cloudflare_enabled
                else configured_id or None
            ),
            "provider_id": provider_id or None,
            "provider_type": provider_type or None,
            "model": model or None,
            "runtime_class": type(provider).__name__ if provider is not None else None,
            "extra": extra,
        }

    @staticmethod
    def _provider_config(provider: Any) -> dict[str, Any]:
        if provider is None:
            return {}
        raw = getattr(provider, "provider_config", {}) or {}
        if isinstance(raw, Mapping):
            return dict(raw)
        return {}

    @staticmethod
    def _model_name(provider: Any, config: dict[str, Any]) -> str:
        if provider is not None:
            get_model = getattr(provider, "get_model", None)
            if callable(get_model):
                try:
                    value = get_model()
                    if value:
                        return str(value)
                except Exception:
                    pass
            value = getattr(provider, "model", None)
            if value:
                return str(value)
        return str(config.get("model") or config.get("model_name") or "")

    @staticmethod
    async def _run_test(role: str, provider: Any) -> dict[str, Any]:
        if role == "embedding":
            vector = await provider.get_embedding("LivingMemory connection test")
            if not isinstance(vector, (list, tuple)) or not vector:
                raise RuntimeError("Embedding 模型返回了空向量")
            if not all(math.isfinite(float(value)) for value in vector):
                raise RuntimeError("Embedding 模型返回了无效向量")
            return {"dimension": len(vector)}

        if role == "rerank":
            results = await provider.rerank(
                "LivingMemory stores long-term memory.",
                [
                    "LivingMemory is a long-term memory plugin.",
                    "The weather is sunny today.",
                ],
                top_n=2,
            )
            if not results:
                raise RuntimeError("Rerank 模型未返回结果")
            top_score = getattr(results[0], "relevance_score", None)
            details: dict[str, Any] = {"result_count": len(results)}
            if top_score is not None:
                details["top_score"] = round(float(top_score), 6)
            return details

        test = getattr(provider, "test", None)
        if callable(test):
            await test()
        else:
            response = await provider.text_chat(
                prompt="REPLY `PONG` ONLY",
                system_prompt="This is a connection test.",
            )
            if response is None:
                raise RuntimeError("LLM 模型未返回响应")
        return {"response_received": True}

    @classmethod
    def _safe_error(cls, initializer: "PluginInitializer", exc: Exception) -> str:
        message = str(exc) or type(exc).__name__
        for secret in cls._collect_secrets(initializer.config_manager.get_all()):
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(
            r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", message
        )
        message = re.sub(
            r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            message,
        )
        message = " ".join(message.split())
        if len(message) > 500:
            message = f"{message[:497]}..."
        return f"{type(exc).__name__}: {message}"

    @classmethod
    def _collect_secrets(cls, value: Any, key: str = "") -> set[str]:
        secrets: set[str] = set()
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                secrets.update(cls._collect_secrets(child_value, str(child_key)))
        elif isinstance(value, (list, tuple)):
            for item in value:
                secrets.update(cls._collect_secrets(item, key))
        elif any(
            mark in key.lower() for mark in ("key", "token", "secret", "password")
        ):
            text = str(value or "")
            if len(text) >= 6:
                secrets.add(text)
        return secrets
