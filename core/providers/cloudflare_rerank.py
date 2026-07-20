"""Async adapter for Cloudflare Workers AI reranker models."""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class CloudflareRerankError(RuntimeError):
    """A safe error that never contains the configured API token."""


@dataclass(frozen=True, slots=True)
class CloudflareRerankResult:
    """AstrBot-compatible rerank result."""

    index: int
    relevance_score: float
    raw_score: float
    score_mapping: str


class CloudflareRerankClient:
    """Call a Workers AI reranker through Cloudflare's account REST API."""

    DEFAULT_MODEL = "@cf/baai/bge-reranker-base"
    DEFAULT_BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        score_mapping: str | None = None,
        apply_sigmoid: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.account_id = str(account_id or "").strip()
        self.api_token = str(
            api_token or os.environ.get("CLOUDFLARE_AUTH_TOKEN") or ""
        ).strip()
        self.model = str(model or self.DEFAULT_MODEL).strip()
        self.base_url = str(base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        configured_mapping = str(score_mapping or "").strip().lower()
        if not configured_mapping:
            if apply_sigmoid is None:
                configured_mapping = "auto"
            else:
                configured_mapping = "sigmoid" if apply_sigmoid else "identity"
        if configured_mapping not in {"auto", "identity", "sigmoid"}:
            raise ValueError(
                "Cloudflare rerank score_mapping must be auto, identity, or sigmoid"
            )
        self.score_mapping = configured_mapping
        # Kept for older callers and the model-inspection API.
        self.apply_sigmoid = configured_mapping == "sigmoid"
        self._transport = transport
        if not self.account_id:
            raise ValueError("Cloudflare account_id is required")
        if not self.api_token:
            raise ValueError(
                "Cloudflare API token is required in configuration or "
                "CLOUDFLARE_AUTH_TOKEN"
            )
        if not self.model:
            raise ValueError("Cloudflare rerank model is required")
        self.provider_config = {
            "id": "cloudflare_workers_ai_rerank",
            "model": self.model,
            "score_mapping": self.score_mapping,
        }

    @property
    def endpoint(self) -> str:
        account_id = quote(self.account_id, safe="")
        model = quote(self.model, safe="@/")
        return f"{self.base_url}/accounts/{account_id}/ai/run/{model}"

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[CloudflareRerankResult]:
        query = str(query or "").strip()
        normalized_documents = [str(item or "").strip() for item in documents]
        if not query:
            raise ValueError("Rerank query must not be empty")
        if not normalized_documents:
            return []
        if any(not item for item in normalized_documents):
            raise ValueError("Rerank documents must not contain empty text")
        limit = (
            len(normalized_documents)
            if top_n is None
            else max(1, min(int(top_n), len(normalized_documents)))
        )
        request_payload = {
            "query": query,
            "contexts": [{"text": item} for item in normalized_documents],
            "top_k": limit,
        }
        payload = await self._post(request_payload)
        rows = self._extract_rows(payload)
        parsed_rows: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_index = row.get("id", row.get("index"))
            raw_score = row.get("score")
            try:
                index = int(raw_index)
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if index in seen or not 0 <= index < len(normalized_documents):
                continue
            if not math.isfinite(score):
                continue
            seen.add(index)
            parsed_rows.append((index, score))

        response_mapping = self._resolve_score_mapping(
            [score for _, score in parsed_rows]
        )
        results: list[CloudflareRerankResult] = []
        for index, score in parsed_rows:
            relevance = (
                self._sigmoid(score) if response_mapping == "sigmoid" else score
            )
            relevance = max(0.0, min(1.0, relevance))
            results.append(
                CloudflareRerankResult(
                    index=index,
                    relevance_score=relevance,
                    raw_score=score,
                    score_mapping=response_mapping,
                )
            )
        results.sort(key=lambda item: item.relevance_score, reverse=True)
        return results[:limit]

    def _resolve_score_mapping(self, scores: list[float]) -> str:
        """Use identity for probability-like responses, sigmoid for logits.

        Workers AI models and API revisions do not all expose scores in the same
        domain.  Resolving once per response preserves ranking and avoids applying
        sigmoid twice to values that are already probabilities.
        """
        if self.score_mapping != "auto":
            return self.score_mapping
        if scores and all(0.0 <= score <= 1.0 for score in scores):
            return "identity"
        return "sigmoid"

    async def _post(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            retryable = False
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self._transport,
                ) as client:
                    response = await client.post(
                        self.endpoint,
                        headers=headers,
                        json=request_payload,
                    )
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code >= 400:
                    last_error = self._http_error(response)
                    if not retryable:
                        raise CloudflareRerankError(last_error)
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise CloudflareRerankError(
                            "Cloudflare rerank returned invalid JSON"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise CloudflareRerankError(
                            "Cloudflare rerank returned an invalid response object"
                        )
                    if payload.get("success") is False:
                        raise CloudflareRerankError(self._api_error(payload))
                    return payload
            except CloudflareRerankError:
                raise
            except httpx.RequestError as exc:
                retryable = True
                last_error = f"Cloudflare rerank request failed: {type(exc).__name__}"
            if retryable and attempt < self.max_retries:
                await asyncio.sleep(self.retry_base_delay * (2**attempt))
                continue
            raise CloudflareRerankError(last_error)
        raise CloudflareRerankError(last_error)

    @staticmethod
    def _extract_rows(payload: dict[str, Any]) -> list[Any]:
        result = payload.get("result", payload)
        if isinstance(result, dict):
            rows = result.get("response", [])
        elif isinstance(result, list):
            rows = result
        else:
            rows = []
        if not isinstance(rows, list):
            raise CloudflareRerankError(
                "Cloudflare rerank response is missing the response array"
            )
        return rows

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            factor = math.exp(-value)
            return 1.0 / (1.0 + factor)
        factor = math.exp(value)
        return factor / (1.0 + factor)

    @staticmethod
    def _http_error(response: httpx.Response) -> str:
        return f"Cloudflare rerank HTTP {response.status_code}"

    def _api_error(self, payload: dict[str, Any]) -> str:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                code = first.get("code")
                message = str(first.get("message") or "request rejected")[:300]
                if self.api_token:
                    message = message.replace(self.api_token, "[REDACTED]")
                return f"Cloudflare rerank API error {code}: {message}"
        return "Cloudflare rerank API rejected the request"


__all__ = [
    "CloudflareRerankClient",
    "CloudflareRerankError",
    "CloudflareRerankResult",
]
