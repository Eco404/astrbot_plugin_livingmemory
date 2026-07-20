from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.topic_settings import (
    TOPIC_SETTING_DEFINITIONS,
    effective_topic_settings,
    topic_setting_defaults,
    validate_topic_setting,
)


def test_effective_topic_settings_are_sparse_overrides_over_code_defaults():
    defaults = topic_setting_defaults()
    effective = effective_topic_settings(
        {"rerank_threshold": 0.61, "llm_concurrency": 4}
    )

    assert effective["rerank_threshold"] == 0.61
    assert effective["llm_concurrency"] == 4
    assert effective["recall_top_k"] == defaults["recall_top_k"]
    assert set(effective) == set(TOPIC_SETTING_DEFINITIONS)


def test_topic_setting_validation_rejects_unknown_and_out_of_range_values():
    with pytest.raises(ValueError, match="Unknown Topic setting"):
        validate_topic_setting("unknown", 1)
    with pytest.raises(ValueError, match="must be <="):
        validate_topic_setting("rerank_concurrency", 33)
    with pytest.raises(ValueError, match="boolean"):
        validate_topic_setting("recall_use_rerank", 1)

