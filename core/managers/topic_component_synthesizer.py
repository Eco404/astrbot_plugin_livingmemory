"""Hierarchical Topic component synthesis."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from astrbot.api import logger

from ..models.topic_memory import TopicFragmentDraft
from .topic_build_contracts import (
    _NARRATIVE_SCHEMA_VERSION,
    TopicBuildValidationError,
)


class TopicComponentSynthesizerMixin:
    async def _synthesize_component(
        self,
        fragments: list[TopicFragmentDraft],
        *,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Synthesize a component without placing an unbounded prompt on the LLM.

        Large semantic components are reduced in bounded batches. Intermediate
        atoms retain their original fragment and fact provenance, so the final
        Topic is still one node rather than several arbitrary size-based Topics.
        """
        batch_size = max(2, int(self.config.get("synthesis_batch_size", 12)))
        total_calls = self._synthesis_call_count(len(fragments), batch_size)
        completed_calls = 0
        all_repairs: list[dict[str, Any]] = []
        progress_lock = asyncio.Lock()

        async def synthesize_batch(
            batch: list[TopicFragmentDraft], level: int
        ) -> dict[str, Any]:
            nonlocal completed_calls
            if len(batch) == 1:
                return self._single_fragment_synthesis(batch[0])
            if progress_callback is not None:
                result = progress_callback(
                    completed_calls,
                    total_calls,
                    len(batch),
                    level,
                )
                if hasattr(result, "__await__"):
                    await result
            synthesis = await self._synthesize_direct(batch)
            async with progress_lock:
                completed_calls += 1
                if progress_callback is not None:
                    result = progress_callback(
                        completed_calls,
                        total_calls,
                        len(batch),
                        level,
                    )
                    if hasattr(result, "__await__"):
                        await result
            return synthesis

        def record_repairs(synthesis: dict[str, Any], level: int) -> None:
            all_repairs.extend(
                {
                    **repair,
                    "synthesis_level": level,
                }
                for repair in synthesis.get("validation_repairs", [])
                if isinstance(repair, dict)
            )

        if len(fragments) <= batch_size:
            synthesis = await synthesize_batch(fragments, 1)
            record_repairs(synthesis, 1)
            synthesis["validation_repairs"] = all_repairs
            return synthesis

        originals = list(fragments)
        first_level_batches = [
            fragments[start : start + batch_size]
            for start in range(0, len(fragments), batch_size)
        ]
        partials = await self._gather_cancel_on_error(
            [synthesize_batch(batch, 1) for batch in first_level_batches]
        )
        for synthesis in partials:
            record_repairs(synthesis, 1)

        level = 2
        while len(partials) > 1:
            reduction_specs: list[dict[str, Any]] = []
            for start in range(0, len(partials), batch_size):
                partial_batch = partials[start : start + batch_size]
                if len(partial_batch) == 1:
                    reduction_specs.append({"passthrough": partial_batch[0]})
                    continue
                pseudo_fragments, fact_map, fragment_map = self._reduction_fragments(
                    partial_batch,
                    run_uid=originals[0].run_uid if originals else "",
                    level=level,
                    offset=start,
                )
                if originals and all(
                    fragment.metadata.get("narrative_schema_version")
                    == _NARRATIVE_SCHEMA_VERSION
                    for fragment in originals
                ):
                    roles = self._fragment_role_payload(originals)
                    for pseudo_fragment in pseudo_fragments:
                        pseudo_fragment.metadata.update(
                            {
                                "narrative_schema_version": (_NARRATIVE_SCHEMA_VERSION),
                                "conversation_roles": roles,
                            }
                        )
                reduction_specs.append(
                    {
                        "pseudo_fragments": pseudo_fragments,
                        "fact_map": fact_map,
                        "fragment_map": fragment_map,
                    }
                )
            pending_specs = [
                spec for spec in reduction_specs if "pseudo_fragments" in spec
            ]
            raw_reductions = await self._gather_cancel_on_error(
                [
                    synthesize_batch(spec["pseudo_fragments"], level)
                    for spec in pending_specs
                ]
            )
            raw_iterator = iter(raw_reductions)
            reduced: list[dict[str, Any]] = []
            for spec in reduction_specs:
                if "passthrough" in spec:
                    reduced.append(spec["passthrough"])
                    continue
                raw_reduction = next(raw_iterator)
                record_repairs(raw_reduction, level)
                reduced.append(
                    self._expand_reduction(
                        raw_reduction,
                        spec["fact_map"],
                        spec["fragment_map"],
                    )
                )
            partials = reduced
            level += 1
        final = self._validate_synthesis(partials[0], originals)
        final_repairs = [
            {
                **repair,
                "synthesis_level": max(1, level - 1),
            }
            for repair in final.get("validation_repairs", [])
            if isinstance(repair, dict)
        ]
        final["validation_repairs"] = [*all_repairs, *final_repairs]
        return final

    async def _synthesize_direct(
        self, fragments: list[TopicFragmentDraft]
    ) -> dict[str, Any]:
        if len(fragments) == 1:
            return self._single_fragment_synthesis(fragments[0])
        payload, fact_refs, actor_refs = self._synthesis_llm_context(fragments)
        prompt = self._synthesis_prompt(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        raw = await self._call_llm(
            prompt,
            self._synthesis_system_prompt(),
            output_contract="synthesis",
        )
        try:
            parsed = self._decode_synthesis_refs(
                self._parse_json_object(raw), fact_refs, actor_refs, fragments
            )
            synthesis = self._validate_synthesis(parsed, fragments)
            self._validate_role_anchored_synthesis(synthesis, fragments)
            return synthesis
        except TopicBuildValidationError as first_exc:
            try:
                repaired_raw = await self._call_llm(
                    self._validation_correction_prompt(prompt, raw, first_exc),
                    self._synthesis_system_prompt(),
                    output_contract="synthesis",
                )
                parsed = self._decode_synthesis_refs(
                    self._parse_json_object(repaired_raw),
                    fact_refs,
                    actor_refs,
                    fragments,
                )
                synthesis = self._validate_synthesis(parsed, fragments)
                self._validate_role_anchored_synthesis(synthesis, fragments)
                logger.info("[TopicMemory] Topic 合成输出经一次校正后通过来源校验")
                return synthesis
            except Exception as repair_exc:
                logger.warning(
                    "[TopicMemory] Topic 合成输出经一次校正后仍无效，"
                    "已使用输入片段确定性重建: first=%s; repair=%s",
                    first_exc,
                    repair_exc,
                )
                parsed = {
                    "validation_repairs": [
                        {
                            "type": "invalid_synthesis_output",
                            "error": str(first_exc)[:500],
                            "correction_error": str(repair_exc)[:500],
                        }
                    ]
                }
        return self._validate_synthesis(parsed, fragments)

    def _single_fragment_synthesis(
        self, fragment: TopicFragmentDraft
    ) -> dict[str, Any]:
        actor_links: list[dict[str, Any]] = []
        seen_actor_links: set[tuple[str, str]] = set()
        for source in (
            fragment.metadata.get("participant_refs", []),
            fragment.metadata.get("mentioned_actor_refs", []),
        ):
            for actor in source:
                if not isinstance(actor, dict):
                    continue
                actor_id = str(actor.get("actor_id") or "").strip()
                relation = str(actor.get("relation_type") or "").strip()
                if (
                    not actor_id
                    or not relation
                    or (actor_id, relation) in seen_actor_links
                ):
                    continue
                seen_actor_links.add((actor_id, relation))
                actor_links.append(
                    {
                        **actor,
                        "actor_id": actor_id,
                        "relation_type": relation,
                        "source_fact_uids": [],
                        "fragment_uids": [fragment.fragment_uid],
                        "timeline_uids": list(fragment.timeline_uids),
                    }
                )
        for fact in fragment.facts:
            fact_uid = str(fact.get("fact_uid") or "").strip()
            for actor in fact.get("actor_refs", []):
                if not isinstance(actor, dict):
                    continue
                actor_id = str(actor.get("actor_id") or "").strip()
                relation = str(actor.get("relation_type") or "").strip()
                if not actor_id or not relation:
                    continue
                existing = next(
                    (
                        item
                        for item in actor_links
                        if item.get("actor_id") == actor_id
                        and item.get("relation_type") == relation
                    ),
                    None,
                )
                if existing is None:
                    existing = {
                        **actor,
                        "actor_id": actor_id,
                        "relation_type": relation,
                        "source_fact_uids": [],
                    }
                    actor_links.append(existing)
                if fact_uid and fact_uid not in existing["source_fact_uids"]:
                    existing["source_fact_uids"].append(fact_uid)
        return {
            "title": fragment.label,
            "summary": fragment.summary,
            "importance": fragment.importance,
            "confidence": fragment.confidence,
            "fragment_uids": [fragment.fragment_uid],
            "atoms": [
                {
                    "type": str(fact.get("type") or "factual"),
                    "content": str(fact["content"]),
                    "importance": float(fact.get("importance", fragment.importance)),
                    "confidence": float(fact.get("confidence", fragment.confidence)),
                    "fragment_uids": [fragment.fragment_uid],
                    "source_fact_uids": [str(fact["fact_uid"])],
                }
                for fact in fragment.facts
            ],
            "actor_links": actor_links,
        }

    @staticmethod
    def _synthesis_call_count(fragment_count: int, batch_size: int) -> int:
        if fragment_count <= 1:
            return 0
        calls = 0
        remaining = fragment_count
        while remaining > 1:
            full_batches, remainder = divmod(remaining, batch_size)
            calls += full_batches + (1 if remainder > 1 else 0)
            remaining = full_batches + (1 if remainder else 0)
        return calls

    def _reduction_fragments(
        self,
        partials: list[dict[str, Any]],
        *,
        run_uid: str,
        level: int,
        offset: int,
    ) -> tuple[
        list[TopicFragmentDraft],
        dict[str, dict[str, Any]],
        dict[str, list[str]],
    ]:
        pseudo_fragments: list[TopicFragmentDraft] = []
        fact_map: dict[str, dict[str, Any]] = {}
        fragment_map: dict[str, list[str]] = {}
        for index, partial in enumerate(partials):
            source_fragment_uids = sorted(
                set(self._unique_strings(partial.get("fragment_uids")))
            )
            partial_key = ":".join(source_fragment_uids)
            fragment_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:topic-reduction:{run_uid}:{level}:"
                    f"{offset + index}:{partial_key}",
                )
            )
            fragment_map[fragment_uid] = source_fragment_uids
            facts: list[dict[str, Any]] = []
            partial_actor_links = [
                dict(value)
                for value in partial.get("actor_links", [])
                if isinstance(value, dict)
            ]
            for atom_index, atom in enumerate(partial.get("atoms", [])):
                if (
                    not isinstance(atom, dict)
                    or not str(atom.get("content") or "").strip()
                ):
                    continue
                fact_uid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"livingmemory:topic-reduction-fact:{fragment_uid}:{atom_index}",
                    )
                )
                fact_map[fact_uid] = atom
                facts.append(
                    {
                        "fact_uid": fact_uid,
                        "type": str(atom.get("type") or "factual"),
                        "content": str(atom["content"]).strip(),
                        "importance": self._score(atom.get("importance"), 0.5),
                        "confidence": self._score(atom.get("confidence"), 0.7),
                        "actor_refs": [
                            {
                                **actor,
                                "source_fact_uids": [fact_uid],
                            }
                            for actor in partial_actor_links
                            if not actor.get("source_fact_uids")
                            or set(self._unique_strings(actor.get("source_fact_uids")))
                            & set(self._unique_strings(atom.get("source_fact_uids")))
                        ],
                    }
                )
            participant_refs = [
                actor
                for actor in partial_actor_links
                if actor.get("relation_type") in {"speaker", "narrator", "responder"}
            ]
            mentioned_actor_refs = [
                actor
                for actor in partial_actor_links
                if actor.get("relation_type")
                not in {"speaker", "narrator", "responder"}
            ]
            pseudo_fragments.append(
                TopicFragmentDraft(
                    fragment_uid=fragment_uid,
                    run_uid=run_uid,
                    candidate_group_uid="topic-reduction",
                    memory_space_id="",
                    label=str(partial.get("title") or "Topic"),
                    summary=str(
                        partial.get("summary") or partial.get("title") or "Topic"
                    ),
                    timeline_uids=[],
                    source_revisions={},
                    facts=facts,
                    importance=self._score(partial.get("importance"), 0.5),
                    confidence=self._score(partial.get("confidence"), 0.7),
                    metadata={
                        "source_fragment_uids": source_fragment_uids,
                        "participant_refs": participant_refs,
                        "mentioned_actor_refs": mentioned_actor_refs,
                    },
                )
            )
        return pseudo_fragments, fact_map, fragment_map

    def _expand_reduction(
        self,
        reduction: dict[str, Any],
        fact_map: dict[str, dict[str, Any]],
        fragment_map: dict[str, list[str]],
    ) -> dict[str, Any]:
        expanded_atoms: list[dict[str, Any]] = []
        for atom in reduction.get("atoms", []):
            source_atoms = [
                fact_map[uid]
                for uid in self._unique_strings(atom.get("source_fact_uids"))
                if uid in fact_map
            ]
            expanded_atoms.append(
                {
                    "type": str(atom.get("type") or "factual"),
                    "content": str(atom.get("content") or "").strip(),
                    "importance": self._score(atom.get("importance"), 0.5),
                    "confidence": self._score(atom.get("confidence"), 0.7),
                    "fragment_uids": sorted(
                        {
                            uid
                            for source in source_atoms
                            for uid in self._unique_strings(source.get("fragment_uids"))
                        }
                    ),
                    "source_fact_uids": sorted(
                        {
                            uid
                            for source in source_atoms
                            for uid in self._unique_strings(
                                source.get("source_fact_uids")
                            )
                        }
                    ),
                }
            )
        expanded = {
            "title": str(reduction.get("title") or "").strip(),
            "summary": str(reduction.get("summary") or "").strip(),
            "importance": self._score(reduction.get("importance"), 0.5),
            "confidence": self._score(reduction.get("confidence"), 0.7),
            "fragment_uids": sorted(
                {
                    uid
                    for pseudo_uid in self._unique_strings(
                        reduction.get("fragment_uids")
                    )
                    for uid in fragment_map.get(pseudo_uid, [])
                }
            ),
            "atoms": expanded_atoms,
            "actor_links": [
                {
                    **actor,
                    "source_fact_uids": sorted(
                        {
                            uid
                            for pseudo_uid in self._unique_strings(
                                actor.get("source_fact_uids")
                            )
                            for uid in self._unique_strings(
                                fact_map.get(pseudo_uid, {}).get("source_fact_uids")
                            )
                        }
                    ),
                }
                for actor in reduction.get("actor_links", [])
                if isinstance(actor, dict)
            ],
        }
        return expanded


__all__ = ["TopicComponentSynthesizerMixin"]
