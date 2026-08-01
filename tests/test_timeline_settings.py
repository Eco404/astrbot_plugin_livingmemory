import pytest
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.timeline_settings import (
    TIMELINE_SETTING_DEFINITIONS,
    effective_timeline_settings,
    timeline_setting_defaults,
    validate_timeline_setting,
    validate_timeline_settings,
)


def test_timeline_defaults_are_complete_and_valid():
    defaults = timeline_setting_defaults()
    assert defaults.keys() == TIMELINE_SETTING_DEFINITIONS.keys()
    assert effective_timeline_settings() == defaults
    for key, value in defaults.items():
        assert validate_timeline_setting(key, value) == value


def test_timeline_setting_validation_rejects_range_and_select_errors():
    with pytest.raises(ValueError):
        validate_timeline_setting("recall_engine.min_relevance_score", 1.1)
    with pytest.raises(ValueError):
        validate_timeline_setting("recall_engine.assistant_context_mode", "maybe")
    with pytest.raises(ValueError):
        validate_timeline_setting("unknown.setting", 1)


def test_config_manager_runtime_overrides_apply_to_get_and_sections():
    manager = ConfigManager({"recall_engine": {"top_k": 7}})
    manager.apply_runtime_overrides(
        {
            "recall_engine.top_k": 3,
            "filtering_settings.use_session_filtering": False,
        }
    )
    assert manager.get("recall_engine.top_k") == 3
    assert manager.recall_engine["top_k"] == 3
    assert manager.filtering_settings["use_session_filtering"] is False


def test_topic_continuation_force_limit_must_exceed_base_rounds():
    values = timeline_setting_defaults()
    values["reflection_engine.summary_trigger_rounds"] = 12
    values["reflection_engine.topic_continuation_force_summary_rounds"] = 12
    with pytest.raises(ValueError, match="必须大于"):
        validate_timeline_settings(values)
    values["reflection_engine.topic_continuation_enabled"] = False
    validate_timeline_settings(values)


def test_dependent_settings_expose_visibility_contract():
    assert TIMELINE_SETTING_DEFINITIONS[
        "reflection_engine.idle_summary_delay_minutes"
    ]["visible_when"] == {
        "key": "reflection_engine.idle_summary_enabled",
        "equals": True,
    }
    assert TIMELINE_SETTING_DEFINITIONS[
        "reflection_engine.topic_continuation_force_summary_rounds"
    ]["visible_when"] == {
        "key": "reflection_engine.topic_continuation_enabled",
        "equals": True,
    }

