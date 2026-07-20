from __future__ import annotations

import json

import httpx
import pytest

from astrbot_plugin_livingmemory.core.providers.cloudflare_rerank import (
    CloudflareRerankClient,
    CloudflareRerankError,
)


@pytest.mark.asyncio
async def test_cloudflare_rerank_maps_ids_and_sigmoid_scores():
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
                        {"id": 0, "score": -2.0},
                        {"id": 1, "score": 2.0},
                    ]
                },
            },
        )

    client = CloudflareRerankClient(
        account_id="account",
        api_token="test-token",
        transport=httpx.MockTransport(handler),
    )
    results = await client.rerank("Which?", ["left", "right"], top_n=2)

    assert captured["authorization"] == "Bearer test-token"
    assert captured["payload"] == {
        "query": "Which?",
        "contexts": [{"text": "left"}, {"text": "right"}],
        "top_k": 2,
    }
    assert [item.index for item in results] == [1, 0]
    assert results[0].relevance_score == pytest.approx(0.880797, rel=1e-5)
    assert results[1].relevance_score == pytest.approx(0.119203, rel=1e-5)


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
        apply_sigmoid=False,
        transport=httpx.MockTransport(handler),
    )
    results = await client.rerank("query", ["document"])

    assert attempts == 2
    assert results[0].index == 0
    assert results[0].relevance_score == 0.25


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
