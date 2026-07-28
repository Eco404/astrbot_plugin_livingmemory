"""Checkpointed Topic fragment extraction and embedding stages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any

from astrbot.api import logger

from ..embedding_signature import (
    TOPIC_FRAGMENT_EMBEDDING_FORMAT,
    make_embedding_signature,
    signature_mismatch_reason,
)
from ..models.conversation_models import (
    build_role_bindings,
    stable_actor_id,
)
from ..models.topic_memory import (
    TimelineTopicCandidate,
    TopicCandidateGroup,
    TopicFragmentDraft,
)
from .topic_build_contracts import (
    _COMPONENT_REVIEW_PROMPT_VERSION,
    _FRAGMENT_PROMPT_VERSION,
    _MATCHING_ALGORITHM_VERSION,
    _SYNTHESIS_PROMPT_VERSION,
    TopicBuildValidationError,
)


class TopicFragmentExtractorMixin:
    async def _extract_groups_concurrently(
        self,
        run_uid: str,
        groups: list[TopicCandidateGroup],
        candidate_map: dict[str, TimelineTopicCandidate],
        *,
        progress_callback=None,
    ) -> None:
        """Extract independent candidate groups concurrently with stable progress."""
        if not groups:
            return
        total_groups = len(groups)
        group_concurrency = max(1, min(self.llm_concurrency, total_groups))
        group_slots = asyncio.Semaphore(group_concurrency)
        progress_lock = asyncio.Lock()
        active_groups: set[int] = set()
        completed_groups = 0

        async def forward_group_progress(event: dict[str, Any]) -> None:
            async with progress_lock:
                forwarded = {
                    **event,
                    "current": completed_groups,
                    "total": total_groups,
                    "completed_groups": completed_groups,
                    "active_group_count": len(active_groups),
                    "group_concurrency": group_concurrency,
                }
                if progress_callback is not None:
                    result = progress_callback(forwarded)
                    if hasattr(result, "__await__"):
                        await result

        async def extract_group(position: int, group: TopicCandidateGroup) -> None:
            nonlocal completed_groups
            async with group_slots:
                async with progress_lock:
                    active_groups.add(position)
                try:
                    await self._extract_group_fragments(
                        run_uid,
                        group,
                        candidate_map,
                        progress_callback=forward_group_progress,
                        group_position=position,
                        group_total=total_groups,
                    )
                finally:
                    async with progress_lock:
                        active_groups.discard(position)
                async with progress_lock:
                    completed_groups += 1
                    await self.store.update_maintenance_run(
                        run_uid,
                        stage="fragment_extraction",
                        current_group_index=completed_groups,
                        total_groups=total_groups,
                    )
                    await self._emit(
                        progress_callback,
                        run_uid,
                        "fragment_extraction",
                        completed_groups,
                        total_groups,
                        activity="group_progress",
                        item_kind="candidate_group",
                        item_index=position,
                        item_total=total_groups,
                        timeline_count=len(group.timeline_uids),
                        completed_groups=completed_groups,
                        active_group_count=len(active_groups),
                        group_concurrency=group_concurrency,
                        llm_concurrency=self.llm_concurrency,
                    )

        await self._gather_cancel_on_error(
            [extract_group(position, group) for position, group in enumerate(groups, 1)]
        )

    async def _extract_group_fragments(
        self,
        run_uid: str,
        group: TopicCandidateGroup,
        candidate_map: dict[str, TimelineTopicCandidate],
        *,
        progress_callback=None,
        group_position: int = 1,
        group_total: int = 1,
    ) -> None:
        inputs = [
            candidate_map[uid] for uid in group.timeline_uids if uid in candidate_map
        ]
        await self._prepare_candidate_evidence(inputs)
        payload = [self._candidate_prompt_payload(item) for item in inputs]
        input_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        input_hash = hashlib.sha256(input_json.encode()).hexdigest()
        identity_hash = self._checkpoint_hash(self._conversation_role_payload(inputs))
        batch_size = max(
            1,
            int(self.config.get("fragment_extraction_batch_size", 12)),
        )
        validation_retries = max(
            0,
            int(self.config.get("fragment_validation_retries", 2)),
        )
        prompt_hash = hashlib.sha256(
            f"{_FRAGMENT_PROMPT_VERSION}\n{batch_size}\n{validation_retries}\n"
            f"{input_hash}\n"
            f"{identity_hash}".encode()
        ).hexdigest()
        provider_id, model_id = self._provider_identity(self.llm_provider)
        claimed = await self.store.begin_group_job(
            run_uid,
            group,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            provider_id=provider_id,
            model_id=model_id,
        )
        if not claimed:
            return
        try:
            fragments: list[TopicFragmentDraft] = []
            total_batches = max(1, math.ceil(len(inputs) / batch_size))
            completed_batches = 0
            progress_lock = asyncio.Lock()
            batch_specs = [
                (batch_index, start, inputs[start : start + batch_size])
                for batch_index, start in enumerate(
                    range(0, len(inputs), batch_size),
                    1,
                )
            ]

            async def call_batch(
                batch_index: int,
                batch: list[TimelineTopicCandidate],
            ) -> tuple[
                str,
                dict[str, str],
                dict[str, dict[str, Any]],
                dict[str, dict[str, Any]],
                str,
            ]:
                nonlocal completed_batches
                await self._emit(
                    progress_callback,
                    run_uid,
                    "fragment_extraction",
                    group_position - 1,
                    group_total,
                    activity="llm_call",
                    item_kind="candidate_group",
                    item_index=group_position,
                    item_total=group_total,
                    timeline_count=len(batch),
                    group_timeline_count=len(inputs),
                    batch_index=batch_index,
                    batch_total=total_batches,
                    llm_call_current=completed_batches,
                    llm_call_total=total_batches,
                    llm_concurrency=self.llm_concurrency,
                )
                llm_payload, timeline_refs, source_refs, actor_refs = (
                    self._fragment_llm_context(batch)
                )
                batch_json = json.dumps(llm_payload, ensure_ascii=False, sort_keys=True)
                prompt = self._fragment_prompt(batch_json)
                raw = await self._call_llm(
                    prompt,
                    self._fragment_system_prompt(),
                    output_contract="fragments",
                )
                async with progress_lock:
                    completed_batches += 1
                    await self._emit(
                        progress_callback,
                        run_uid,
                        "fragment_extraction",
                        group_position - 1,
                        group_total,
                        activity="llm_call",
                        item_kind="candidate_group",
                        item_index=group_position,
                        item_total=group_total,
                        timeline_count=len(batch),
                        group_timeline_count=len(inputs),
                        batch_index=batch_index,
                        batch_total=total_batches,
                        llm_call_current=completed_batches,
                        llm_call_total=total_batches,
                        llm_concurrency=self.llm_concurrency,
                    )
                return raw, timeline_refs, source_refs, actor_refs, prompt

            raw_outputs = await self._gather_cancel_on_error(
                [
                    call_batch(batch_index, batch)
                    for batch_index, _, batch in batch_specs
                ]
            )
            for (_, _, batch), output in zip(batch_specs, raw_outputs, strict=True):
                raw, timeline_refs, source_refs, actor_refs, prompt = output
                batch_payload, _, _, _ = self._fragment_llm_context(batch)
                batch_input_hash, batch_prompt_hash = self._fragment_request_hashes(
                    batch_payload,
                    prompt,
                )
                fragment_index_offset = len(fragments)
                try:
                    parsed = self._parse_json_object(raw)
                    requested_refs = self._requested_evidence_refs(
                        parsed, timeline_refs
                    )
                    if requested_refs:
                        await self._attach_requested_evidence(
                            batch,
                            requested_refs,
                            timeline_refs,
                        )
                        (
                            evidence_payload,
                            timeline_refs,
                            source_refs,
                            actor_refs,
                        ) = self._fragment_llm_context(batch)
                        prompt = self._fragment_prompt(
                            json.dumps(
                                evidence_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )
                        batch_payload = evidence_payload
                        prompt += (
                            "\n\nThe requested raw evidence has now been attached. "
                            "Return the final fragments and leave evidence_requests "
                            "empty."
                        )
                        batch_input_hash, batch_prompt_hash = (
                            self._fragment_request_hashes(
                                batch_payload,
                                prompt,
                            )
                        )
                        raw = await self._call_llm(
                            prompt,
                            self._fragment_system_prompt(),
                            output_contract="fragments",
                        )
                        parsed = self._parse_json_object(raw)
                    parsed = self._decode_fragment_refs(
                        parsed,
                        timeline_refs,
                        source_refs,
                        actor_refs,
                        require_source_accounting=True,
                    )
                    batch_fragments = self._validate_fragments(
                        parsed,
                        run_uid,
                        group,
                        batch,
                        batch_prompt_hash,
                        batch_input_hash,
                        provider_id,
                        model_id,
                        fragment_index_offset=fragment_index_offset,
                    )
                except TopicBuildValidationError as first_exc:
                    validation_error: Exception = first_exc
                    previous_output = raw
                    batch_fragments = []
                    for correction_index in range(1, validation_retries + 1):
                        repaired_raw = ""
                        try:
                            correction_prompt = self._validation_correction_prompt(
                                prompt, previous_output, validation_error
                            )
                            batch_input_hash, batch_prompt_hash = (
                                self._fragment_request_hashes(
                                    batch_payload,
                                    correction_prompt,
                                )
                            )
                            repaired_raw = await self._call_llm(
                                correction_prompt,
                                self._fragment_system_prompt(),
                                output_contract="fragments",
                            )
                            repaired = self._decode_fragment_refs(
                                self._parse_json_object(repaired_raw),
                                timeline_refs,
                                source_refs,
                                actor_refs,
                                require_source_accounting=True,
                            )
                            batch_fragments = self._validate_fragments(
                                repaired,
                                run_uid,
                                group,
                                batch,
                                batch_prompt_hash,
                                batch_input_hash,
                                provider_id,
                                model_id,
                                fragment_index_offset=fragment_index_offset,
                            )
                        except Exception as repair_exc:
                            validation_error = repair_exc
                            if repaired_raw:
                                previous_output = repaired_raw
                            continue
                        logger.info(
                            "[TopicMemory] 片段提取输出经 %s 次校正后通过来源校验",
                            correction_index,
                        )
                        break
                    if not batch_fragments:
                        logger.warning(
                            "[TopicMemory] 片段提取输出经 %s 次校正后仍无法通过来源校验，"
                            "已回退到输入 Timeline 的确定性片段: first=%s; last=%s",
                            validation_retries,
                            first_exc,
                            validation_error,
                        )
                        batch_fragments = self._fallback_fragments(
                            run_uid,
                            group,
                            batch,
                            batch_prompt_hash,
                            batch_input_hash,
                            provider_id,
                            model_id,
                            fragment_index_offset=fragment_index_offset,
                            reason=(
                                f"{first_exc}; corrections={validation_retries}; "
                                f"last={validation_error}"
                            ),
                        )
                fragments.extend(batch_fragments)
            await self.store.replace_group_fragments(
                run_uid, group.group_uid, fragments
            )
            await self.store.finish_group_job(run_uid, group.group_uid)
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.finish_group_job(
                    run_uid,
                    group.group_uid,
                    error="cancelled before group extraction completed",
                )
            )
            raise

        except Exception as exc:
            await self.store.finish_group_job(
                run_uid,
                group.group_uid,
                error=str(exc),
            )
            raise

    def _fragment_request_hashes(
        self,
        payload: dict[str, Any],
        prompt: str,
    ) -> tuple[str, str]:
        input_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        input_hash = hashlib.sha256(input_json.encode()).hexdigest()
        prompt_hash = hashlib.sha256(
            (
                f"{_FRAGMENT_PROMPT_VERSION}\n"
                f"{self._fragment_system_prompt()}\n{prompt}"
            ).encode()
        ).hexdigest()
        return input_hash, prompt_hash

    @staticmethod
    def _requested_evidence_refs(
        parsed: dict[str, Any],
        timeline_refs: dict[str, str],
    ) -> set[str]:
        requested: set[str] = set()
        raw_fragments = parsed.get("fragments")
        if not isinstance(raw_fragments, list):
            return requested
        for fragment in raw_fragments:
            if not isinstance(fragment, dict):
                continue
            requests = fragment.get("evidence_requests")
            if not isinstance(requests, list):
                continue
            for request in requests:
                if not isinstance(request, dict):
                    continue
                ref = str(request.get("timeline_ref") or "").strip()
                if ref in timeline_refs:
                    requested.add(ref)
        return requested

    async def _attach_requested_evidence(
        self,
        inputs: list[TimelineTopicCandidate],
        requested_refs: set[str],
        timeline_refs: dict[str, str],
    ) -> None:
        """Fulfil one model-requested evidence round without changing memory facts."""
        requested_uids = {
            timeline_refs[ref] for ref in requested_refs if ref in timeline_refs
        }
        by_uid = {item.memory_uid: item for item in inputs}
        limit = max(1, min(200, int(self.config.get("evidence_max_messages", 80))))
        for timeline_uid in requested_uids:
            item = by_uid.get(timeline_uid)
            if item is None:
                continue
            if item.features.get("raw_evidence"):
                item.features["evidence_status"] = "llm_requested_attached"
                continue
            if self.conversation_store is None or not item.session_id:
                item.features["evidence_status"] = "llm_requested_unavailable"
                continue
            first_id = item.source_window.get("first_message_id")
            last_id = item.source_window.get("last_message_id")
            if first_id is None or last_id is None:
                item.features["evidence_status"] = "llm_requested_unavailable"
                continue
            try:
                messages = await self.conversation_store.get_messages_by_id_span(
                    item.session_id,
                    int(first_id),
                    int(last_id),
                    limit=limit,
                )
            except Exception:
                logger.warning(
                    "[TopicMemory] LLM 请求补证时读取原始消息失败: %s",
                    timeline_uid,
                    exc_info=True,
                )
                messages = []
            if not messages:
                item.features["evidence_status"] = "llm_requested_unavailable"
                continue
            if not item.role_bindings.get("actors"):
                item.role_bindings = build_role_bindings(messages, item.persona_id)
            item.features["raw_evidence"] = [
                {
                    "message_id": message.id,
                    "role": message.role,
                    "actor_id": stable_actor_id(
                        message.platform,
                        message.sender_id,
                        "assistant"
                        if message.role == "assistant"
                        or message.metadata.get("is_bot_message")
                        else "human",
                    ),
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "timestamp": message.timestamp,
                    "content": str(message.content)[:2000],
                }
                for message in messages
            ]
            item.features["evidence_status"] = "llm_requested_attached"

    async def _embed_fragments(
        self, fragments: list[TopicFragmentDraft], progress_callback=None
    ) -> list[TopicFragmentDraft]:
        missing = [
            item
            for item in fragments
            if not item.embedding
            or signature_mismatch_reason(
                item.embedding_signature,
                self.embedding_provider,
                expected_formats={TOPIC_FRAGMENT_EMBEDDING_FORMAT},
            )
            is not None
        ]
        if missing and self.embedding_provider is None:
            raise RuntimeError("Topic build requires an Embedding Provider")
        batch_size = max(1, int(self.config.get("embedding_batch_size", 8)))
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            texts = [self._fragment_embedding_text(item) for item in batch]
            vectors = await self._get_embeddings(texts)
            if len(vectors) != len(batch):
                raise RuntimeError(
                    "Embedding Provider returned an unexpected vector count"
                )
            for fragment, vector in zip(batch, vectors, strict=True):
                normalized = [float(value) for value in vector]
                fragment.embedding = normalized
                fragment.embedding_signature = make_embedding_signature(
                    self.embedding_provider,
                    dimension=len(normalized),
                    input_format_version=TOPIC_FRAGMENT_EMBEDDING_FORMAT,
                )
                await self.store.update_fragment_embedding(
                    fragment.fragment_uid,
                    normalized,
                    fragment.embedding_signature,
                )
            await self._emit(
                progress_callback,
                batch[0].run_uid if batch else "",
                "embedding",
                min(start + len(batch), len(missing)),
                len(missing),
            )
        return (
            await self.store.list_fragments(run_uid=fragments[0].run_uid)
            if fragments
            else []
        )

    async def _match_fragments_checkpointed(
        self,
        run_uid: str,
        fragments: list[TopicFragmentDraft],
        *,
        progress_callback=None,
    ) -> tuple[list[list[int]], dict[str, float]]:
        checkpoint_key = "fragment_matching"
        input_hash = self._checkpoint_hash(
            {
                "fragments": [
                    {
                        "fragment_uid": item.fragment_uid,
                        "embedding": item.embedding,
                        "label": item.label,
                    }
                    for item in fragments
                ],
                "fragment_similarity_threshold": self.config.get(
                    "fragment_similarity_threshold", 0.78
                ),
                "candidate_similarity_threshold": self.config.get(
                    "candidate_similarity_threshold", 0.52
                ),
                "rerank_candidate_floor": self.config.get(
                    "rerank_candidate_floor", 0.63
                ),
                "component_min_pair_similarity": self.config.get(
                    "component_min_pair_similarity", 0.52
                ),
                "component_min_average_similarity": self.config.get(
                    "component_min_average_similarity", 0.65
                ),
                "component_size_cohesion_penalty": self.config.get(
                    "component_size_cohesion_penalty", 0.005
                ),
                "rerank_threshold": self.config.get("rerank_threshold", 0.55),
                "rerank_reciprocal_rank_threshold": self.config.get(
                    "rerank_reciprocal_rank_threshold", 0.60
                ),
                "rerank_top_n": self.config.get("rerank_top_n", 5),
                "rerank_provider": self._provider_identity(self.rerank_provider),
                "matching_algorithm_version": _MATCHING_ALGORITHM_VERSION,
            }
        )
        checkpoint = await self.store.get_build_checkpoint(run_uid, checkpoint_key)
        if checkpoint and checkpoint.get("input_hash") == input_hash:
            payload = checkpoint.get("payload") or {}
            component_uids = payload.get("components")
            scores = payload.get("scores")
            index_by_uid = {
                item.fragment_uid: index for index, item in enumerate(fragments)
            }
            if isinstance(component_uids, list) and isinstance(scores, dict):
                flattened = [
                    str(uid)
                    for component in component_uids
                    if isinstance(component, list)
                    for uid in component
                ]
                if len(flattened) == len(set(flattened)) and set(flattened) == set(
                    index_by_uid
                ):
                    await self._emit(
                        progress_callback,
                        run_uid,
                        "fragment_matching",
                        len(fragments),
                        len(fragments),
                        checkpoint_reused=True,
                    )
                    return (
                        [
                            [index_by_uid[str(uid)] for uid in component]
                            for component in component_uids
                        ],
                        {str(key): float(value) for key, value in scores.items()},
                    )
        components, scores = await self._match_fragments(
            fragments,
            progress_callback=progress_callback,
        )
        await self.store.save_build_checkpoint(
            run_uid=run_uid,
            checkpoint_key=checkpoint_key,
            stage="fragment_matching",
            input_hash=input_hash,
            payload={
                "components": [
                    [fragments[index].fragment_uid for index in component]
                    for component in components
                ],
                "scores": scores,
                "quality": self._matching_quality(components, len(fragments)),
                "audit": self._matching_audit(fragments, components, scores),
                "matching_algorithm_version": _MATCHING_ALGORITHM_VERSION,
            },
        )
        return components, scores

    async def _synthesize_component_checkpointed(
        self,
        run_uid: str,
        fragments: list[TopicFragmentDraft],
        *,
        progress_callback=None,
    ) -> dict[str, Any]:
        component_key = hashlib.sha256(
            "\n".join(sorted(item.fragment_uid for item in fragments)).encode()
        ).hexdigest()
        checkpoint_key = f"topic_synthesis:{component_key}"
        provider_id, model_id = self._provider_identity(self.llm_provider)
        input_hash = self._checkpoint_hash(
            {
                "prompt_version": _SYNTHESIS_PROMPT_VERSION,
                "provider_id": provider_id,
                "model_id": model_id,
                "synthesis_batch_size": self.config.get("synthesis_batch_size", 12),
                "supplemental_identity_hints": self._fragment_identity_payload(
                    fragments
                ),
                "conversation_roles": self._fragment_role_payload(fragments),
                "fragments": [
                    self._fragment_synthesis_payload(item) for item in fragments
                ],
            }
        )
        checkpoint = await self.store.get_build_checkpoint(run_uid, checkpoint_key)
        if checkpoint and checkpoint.get("input_hash") == input_hash:
            payload = checkpoint.get("payload")
            if isinstance(payload, dict):
                try:
                    synthesis = self._validate_synthesis(payload, fragments)
                    self._validate_role_anchored_synthesis(synthesis, fragments)
                except TopicBuildValidationError:
                    synthesis = None
                if synthesis is not None:
                    if progress_callback is not None:
                        result = progress_callback(1, 1, len(fragments), 0)
                        if hasattr(result, "__await__"):
                            await result
                    synthesis["checkpoint_reused"] = True
                    return synthesis
        synthesis = await self._synthesize_component(
            fragments,
            progress_callback=progress_callback,
        )
        await self.store.save_build_checkpoint(
            run_uid=run_uid,
            checkpoint_key=checkpoint_key,
            stage="topic_synthesis",
            input_hash=input_hash,
            payload=synthesis,
            metadata={"fragment_count": len(fragments)},
        )
        return synthesis

    async def _review_components_checkpointed(
        self,
        run_uid: str,
        fragments: list[TopicFragmentDraft],
        components: list[list[int]],
        *,
        progress_callback=None,
    ) -> list[list[int]]:
        """Let the LLM split structurally mixed components before synthesis.

        Embedding and rerank remain responsible for candidate connectivity.  This
        stage only reviews components large enough to hide multiple retrieval
        intents, and it may never add, drop, or duplicate a fragment.
        """
        total = len(components)
        if not components:
            await self._emit(
                progress_callback,
                run_uid,
                "component_review",
                0,
                0,
            )
            return components

        enabled = bool(self.config.get("component_review_enabled", True))
        minimum = max(
            3,
            int(self.config.get("component_review_min_fragments", 6)),
        )
        maximum = max(
            minimum,
            int(self.config.get("component_review_max_fragments", 48)),
        )
        if not enabled:
            await self._emit(
                progress_callback,
                run_uid,
                "component_review",
                total,
                total,
                activity="disabled",
                item_kind="component_review",
                reviewed_components=total,
                component_review_concurrency=self.llm_concurrency,
            )
            return components

        review_slots = asyncio.Semaphore(max(1, min(self.llm_concurrency, total)))
        progress_lock = asyncio.Lock()
        completed = 0
        active = 0

        async def emit_progress(
            position: int,
            fragment_count: int,
            *,
            active_delta: int = 0,
            completed_delta: int = 0,
            output_groups: int = 0,
            activity: str = "stage_progress",
        ) -> None:
            nonlocal active, completed
            async with progress_lock:
                active += active_delta
                completed += completed_delta
                await self._emit(
                    progress_callback,
                    run_uid,
                    "component_review",
                    completed,
                    total,
                    activity=activity,
                    item_kind="component_review",
                    item_index=position,
                    item_total=total,
                    fragment_count=fragment_count,
                    reviewed_components=completed,
                    active_component_review_count=active,
                    component_review_concurrency=self.llm_concurrency,
                    review_output_groups=output_groups,
                    llm_concurrency=self.llm_concurrency,
                )

        async def review_one(
            position: int,
            component: list[int],
        ) -> list[list[int]]:
            component_fragments = [fragments[index] for index in component]
            if len(component) < minimum:
                await emit_progress(
                    position,
                    len(component),
                    completed_delta=1,
                    output_groups=1,
                    activity="below_review_threshold",
                )
                return [component]
            if len(component) > maximum:
                logger.warning(
                    "[TopicMemory] 组件片段数超过单次结构复核上限，保留原组件 "
                    "(run_uid=%s, fragments=%s, limit=%s)",
                    run_uid,
                    len(component),
                    maximum,
                )
                await emit_progress(
                    position,
                    len(component),
                    completed_delta=1,
                    output_groups=1,
                    activity="above_review_limit",
                )
                return [component]

            async with review_slots:
                await emit_progress(
                    position,
                    len(component),
                    active_delta=1,
                    activity="llm_call",
                )
                try:
                    uid_groups = await self._review_component_checkpointed(
                        run_uid,
                        component_fragments,
                    )
                finally:
                    # Completion is emitted after validation so the displayed group
                    # count always describes the actual result.
                    await emit_progress(
                        position,
                        len(component),
                        active_delta=-1,
                        activity="llm_call_completed",
                    )
            index_by_uid = {fragments[index].fragment_uid: index for index in component}
            reviewed = [[index_by_uid[uid] for uid in group] for group in uid_groups]
            await emit_progress(
                position,
                len(component),
                completed_delta=1,
                output_groups=len(reviewed),
                activity="stage_progress",
            )
            return reviewed

        reviewed_components = await self._gather_cancel_on_error(
            [
                review_one(position, component)
                for position, component in enumerate(components, 1)
            ]
        )
        flattened = [
            group
            for reviewed_component in reviewed_components
            for group in reviewed_component
        ]
        if sorted(index for group in flattened for index in group) != list(
            range(len(fragments))
        ):
            raise TopicBuildValidationError(
                "component review did not preserve the complete fragment scope"
            )
        return flattened

    async def _review_component_checkpointed(
        self,
        run_uid: str,
        fragments: list[TopicFragmentDraft],
    ) -> list[list[str]]:
        component_key = hashlib.sha256(
            "\n".join(sorted(item.fragment_uid for item in fragments)).encode()
        ).hexdigest()
        checkpoint_key = f"component_review:{component_key}"
        provider_id, model_id = self._provider_identity(self.llm_provider)
        input_payload, fragment_refs = self._component_review_llm_context(fragments)
        input_hash = self._checkpoint_hash(
            {
                "prompt_version": _COMPONENT_REVIEW_PROMPT_VERSION,
                "provider_id": provider_id,
                "model_id": model_id,
                "fragments": input_payload,
            }
        )
        checkpoint = await self.store.get_build_checkpoint(run_uid, checkpoint_key)
        if checkpoint and checkpoint.get("input_hash") == input_hash:
            payload = checkpoint.get("payload") or {}
            try:
                return self._validate_component_uid_groups(
                    payload.get("groups"),
                    fragments,
                )
            except TopicBuildValidationError as exc:
                # A damaged or partially written checkpoint must not make a
                # resumable build permanently unrecoverable. Recompute it and
                # overwrite the checkpoint with a validated payload.
                logger.warning(
                    "[TopicMemory] 组件结构复核检查点无效，将重新计算 "
                    "(run_uid=%s, checkpoint=%s): %s",
                    run_uid,
                    checkpoint_key,
                    exc,
                )

        fallback_reason = ""
        try:
            groups = await self._review_component_direct(
                fragments,
                input_payload=input_payload,
                fragment_refs=fragment_refs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not bool(self.config.get("component_review_failure_fallback", True)):
                raise
            fallback_reason = str(exc)[:500]
            groups = [[item.fragment_uid for item in fragments]]
            logger.warning(
                "[TopicMemory] 组件结构复核失败，保留原组件 "
                "(run_uid=%s, fragments=%s): %s",
                run_uid,
                len(fragments),
                fallback_reason,
            )

        await self.store.save_build_checkpoint(
            run_uid=run_uid,
            checkpoint_key=checkpoint_key,
            stage="component_review",
            input_hash=input_hash,
            payload={
                "groups": groups,
                "fallback_reason": fallback_reason,
                "prompt_version": _COMPONENT_REVIEW_PROMPT_VERSION,
            },
            metadata={
                "input_fragment_count": len(fragments),
                "output_group_count": len(groups),
                "fallback": bool(fallback_reason),
            },
        )
        return groups

    async def _review_component_direct(
        self,
        fragments: list[TopicFragmentDraft],
        *,
        input_payload: dict[str, Any] | None = None,
        fragment_refs: dict[str, str] | None = None,
    ) -> list[list[str]]:
        if input_payload is None or fragment_refs is None:
            input_payload, fragment_refs = self._component_review_llm_context(fragments)
        input_json = json.dumps(input_payload, ensure_ascii=False)
        prompt = self._component_review_prompt(input_json)
        raw = await self._call_llm(
            prompt,
            self._component_review_system_prompt(),
            output_contract="component_review",
        )
        try:
            parsed = self._parse_json_object(raw)
            return self._decode_component_review_refs(
                parsed,
                fragment_refs,
                fragments,
            )
        except TopicBuildValidationError as first_exc:
            correction = self._validation_correction_prompt(
                prompt,
                raw,
                first_exc,
            )
            corrected = await self._call_llm(
                correction,
                self._component_review_system_prompt(),
                output_contract="component_review",
            )
            parsed = self._parse_json_object(corrected)
            return self._decode_component_review_refs(
                parsed,
                fragment_refs,
                fragments,
            )


__all__ = ["TopicFragmentExtractorMixin"]
