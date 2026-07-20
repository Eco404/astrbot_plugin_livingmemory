"""Tests for the WebUI model inspection and connection-test handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.page_api_modules.model_handler import ModelHandler
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils


class DummyLLM:
    provider_config = {
        "id": "default-chat",
        "type": "openai_chat_completion",
        "model": "fallback-model",
    }

    def get_model(self):
        return "live-chat-model"

    async def test(self):
        return None


class DummyEmbedding:
    provider_config = {
        "id": "default-embedding",
        "type": "openai_embedding",
        "model": "text-embedding",
    }

    def get_model(self):
        return "text-embedding"

    def get_dim(self):
        return 3

    async def get_embedding(self, _text):
        return [0.1, 0.2, 0.3]


def make_initializer(config=None):
    return SimpleNamespace(
        config_manager=ConfigManager(config or {}),
        llm_provider=DummyLLM(),
        embedding_provider=DummyEmbedding(),
        rerank_provider=None,
    )


@pytest.mark.asyncio
async def test_list_models_exposes_resolved_astrbot_defaults():
    handler = ModelHandler(PageApiUtils())

    response = await handler.list_models(make_initializer())

    assert response["status"] == "ok"
    models = {item["role"]: item for item in response["data"]["models"]}
    assert models["llm"]["selection"] == "astrbot_default"
    assert models["llm"]["provider_id"] == "default-chat"
    assert models["llm"]["model"] == "live-chat-model"
    assert models["embedding"]["extra"]["dimension"] == 3
    assert models["rerank"]["selection"] == "vector_only"
    assert models["rerank"]["testable"] is False


@pytest.mark.asyncio
async def test_list_models_exposes_cloudflare_configuration_without_secrets():
    handler = ModelHandler(PageApiUtils())
    initializer = make_initializer(
        {
            "cloudflare_rerank": {
                "enabled": True,
                "account_id": "account-id",
                "api_token": "private-token",
                "model": "@cf/baai/bge-reranker-base",
            }
        }
    )
    initializer.rerank_provider = SimpleNamespace(
        provider_config={
            "id": "cloudflare_workers_ai_rerank",
            "model": "@cf/baai/bge-reranker-base",
        },
        model="@cf/baai/bge-reranker-base",
        base_url="https://api.cloudflare.com/client/v4",
    )

    response = await handler.list_models(initializer)

    rerank = response["data"]["models"][2]
    assert rerank["selection"] == "cloudflare"
    assert rerank["configured_provider_id"] == "cloudflare_workers_ai_rerank"
    assert rerank["extra"]["credential_source"] == "configuration"
    assert "score_mapping" not in rerank["extra"]
    assert "private-token" not in str(rerank)


@pytest.mark.asyncio
async def test_list_models_marks_configured_provider_fallback():
    handler = ModelHandler(PageApiUtils())
    initializer = make_initializer(
        {"provider_settings": {"llm_provider_id": "missing-chat"}}
    )

    response = await handler.list_models(initializer)

    llm = response["data"]["models"][0]
    assert llm["configured_provider_id"] == "missing-chat"
    assert llm["provider_id"] == "default-chat"
    assert llm["selection"] == "fallback"


@pytest.mark.asyncio
async def test_list_models_explains_cloudflare_initialization_failure():
    handler = ModelHandler(PageApiUtils())
    initializer = make_initializer(
        {"cloudflare_rerank": {"enabled": True, "account_id": "account-id"}}
    )
    initializer.rerank_initialization_error = (
        "Cloudflare API token is required in configuration or environment"
    )

    response = await handler.list_models(initializer)

    rerank = response["data"]["models"][2]
    assert rerank["available"] is False
    assert rerank["selection"] == "unavailable"
    assert rerank["configured_provider_id"] == "cloudflare_workers_ai_rerank"
    assert "token is required" in rerank["extra"]["initialization_error"]


@pytest.mark.asyncio
async def test_embedding_connection_test_reports_dimension(monkeypatch):
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"role": "embedding"})
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.model_handler.request",
        request,
    )
    handler = ModelHandler(PageApiUtils())

    response = await handler.test_connection(make_initializer())

    assert response["status"] == "ok"
    assert response["data"]["success"] is True
    assert response["data"]["details"] == {"dimension": 3}
    assert response["data"]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_connection_error_redacts_configured_token(monkeypatch):
    token = "super-secret-token"

    class FailingEmbedding(DummyEmbedding):
        async def get_embedding(self, _text):
            raise RuntimeError(f"request rejected: token={token}")

    request = MagicMock()
    request.get_json = AsyncMock(return_value={"role": "embedding"})
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.model_handler.request",
        request,
    )
    initializer = make_initializer(
        {"cloudflare_rerank": {"api_token": token}}
    )
    initializer.embedding_provider = FailingEmbedding()

    response = await ModelHandler(PageApiUtils()).test_connection(initializer)

    assert response["status"] == "error"
    assert token not in response["message"]
    assert "[REDACTED]" in response["message"]
