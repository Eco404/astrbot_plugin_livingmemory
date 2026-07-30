from __future__ import annotations

from collections import Counter

from astrbot_plugin_livingmemory.core.managers.topic_build_contracts import (
    TopicBuildValidationError as ContractValidationError,
)
from astrbot_plugin_livingmemory.core.managers.topic_build_manager import (
    TopicBuildManager,
    TopicBuildValidationError,
)
from astrbot_plugin_livingmemory.core.managers.topic_component_matcher import (
    TopicComponentMatcherMixin,
)
from astrbot_plugin_livingmemory.core.managers.topic_component_synthesizer import (
    TopicComponentSynthesizerMixin,
)
from astrbot_plugin_livingmemory.core.managers.topic_fragment_extractor import (
    TopicFragmentExtractorMixin,
)
from astrbot_plugin_livingmemory.core.managers.topic_maintenance_manager import (
    TopicMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.managers.topic_snapshot_publisher import (
    TopicSnapshotPublisherMixin,
)
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallPipeline
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import TopicRetriever
from astrbot_plugin_livingmemory.core.topic_similarity import (
    canonical_text,
    lexical_tokens,
    retrieval_text_features,
    weighted_jaccard_similarity,
)


def test_topic_build_manager_delegates_major_pipeline_stages() -> None:
    assert (
        TopicBuildManager._extract_group_fragments
        is TopicFragmentExtractorMixin._extract_group_fragments
    )
    assert (
        TopicBuildManager._match_fragments
        is TopicComponentMatcherMixin._match_fragments
    )
    assert (
        TopicBuildManager._synthesize_component
        is TopicComponentSynthesizerMixin._synthesize_component
    )
    assert (
        TopicBuildManager._materialize_snapshot
        is TopicSnapshotPublisherMixin._materialize_snapshot
    )


def test_topic_build_validation_error_remains_a_compatible_reexport() -> None:
    assert TopicBuildValidationError is ContractValidationError


def test_legacy_text_helpers_delegate_to_shared_contracts() -> None:
    value = "  ＡstrBot 记忆／Topic_测试  "

    assert TopicMaintenanceManager.normalize_text(value) == canonical_text(value)
    assert TopicMaintenanceManager.tokenize(value) == lexical_tokens(value)


def test_weighted_jaccard_compatibility_wrapper_uses_shared_contract() -> None:
    left = {"报销", "salary", "2026"}
    right = {"报销", "salary", "june"}
    frequency = Counter({"报销": 2, "salary": 3, "2026": 8, "june": 1})

    expected = weighted_jaccard_similarity(left, right, frequency, 10)
    actual = TopicBuildManager._weighted_jaccard(left, right, frequency, 10)

    assert actual == expected


def test_recall_feature_wrappers_share_one_contract() -> None:
    value = "AstrBot memory 报销补记"
    expected = retrieval_text_features(value)

    assert RecallPipeline._text_features(value) == expected
    assert TopicRetriever._text_features(value) == expected
