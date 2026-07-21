import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.timeline_settings import (
    TIMELINE_SETTING_DEFINITIONS,
    effective_timeline_settings,
    timeline_setting_defaults,
    validate_timeline_setting,
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

