from __future__ import annotations

import json

import httpx
import pytest

from astrbot_plugin_livingmemory.core.providers.cloudflare_rerank import (
    CloudflareRerankClient,
    CloudflareRerankError,
)


@pytest.mark.asyncio
async def test_cloudflare_rerank_maps_ids_and_preserves_probability_scores():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "response": [
                        {"id": 0, "score": 0.12},
                        {"id": 1, "score": 0.88},
                    ]
                },
            },
        )

    client = CloudflareRerankClient(
        account_id="account",
        api_token="secret-token",
        transport=httpx.MockTransport(handler),
    )
    results = await client.rerank("Which?", ["left", "right"], top_n=2)

    assert captured["authorization"] == "Bearer secret-token"
    assert captured["payload"] == {
        "query": "Which?",
        "contexts": [{"text": "left"}, {"text": "right"}],
        "top_k": 2,
    }
    assert [item.index for item in results] == [1, 0]
    assert results[0].relevance_score == 0.88
    assert results[1].relevance_score == 0.12


@pytest.mark.asyncio
async def test_cloudflare_rerank_retries_429_and_accepts_direct_worker_shape():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"success": False})
        return httpx.Response(
            200,
            json={"response": [{"id": 0, "score": 0.25}]},
        )

    client = CloudflareRerankClient(
        account_id="account",
        api_token="token",
        max_retries=1,
        retry_base_delay=0,
        transport=httpx.MockTransport(handler),
    )
    results = await client.rerank("query", ["document"])

    assert attempts == 2
    assert results[0].index == 0
    assert results[0].relevance_score == 0.25


@pytest.mark.asyncio
async def test_cloudflare_rerank_preserves_raw_score_for_audit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": [{"id": 0, "score": 0.72}]},
        )

    client = CloudflareRerankClient(
        account_id="account",
        api_token="token",
        transport=httpx.MockTransport(handler),
    )
    results = await client.rerank("query", ["document"])

    assert results[0].relevance_score == 0.72
    assert results[0].raw_score == 0.72


@pytest.mark.asyncio
async def test_cloudflare_rerank_rejects_scores_outside_probability_range():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": [{"id": 0, "score": 1.2}]},
        )

    client = CloudflareRerankClient(
        account_id="account",
        api_token="token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CloudflareRerankError, match=r"outside \[0, 1\]"):
        await client.rerank("query", ["document"])


@pytest.mark.asyncio
async def test_cloudflare_rerank_error_does_not_expose_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "authentication failed"}],
            },
        )

    token = "must-not-leak"
    client = CloudflareRerankClient(
        account_id="account",
        api_token=token,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CloudflareRerankError) as captured:
        await client.rerank("query", ["document"])

    assert token not in str(captured.value)
    assert "HTTP 403" in str(captured.value)
