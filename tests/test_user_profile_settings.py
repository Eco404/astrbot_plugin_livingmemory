from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.user_profile_settings import (
    USER_PROFILE_SETTING_DEFINITIONS,
    USER_PROFILE_SETTINGS_REVISION,
    effective_user_profile_settings,
    user_profile_setting_defaults,
    validate_user_profile_setting,
)


def test_user_profile_settings_have_complete_metadata_and_defaults():
    defaults = user_profile_setting_defaults()

    assert USER_PROFILE_SETTINGS_REVISION == 5
    assert defaults["user_profile.legacy_summary_candidate_confidence"] == 0.45
    assert defaults["user_profile.legacy_relationship_initial_dimension_cap"] == 0.35
    assert defaults["user_profile.enabled"] is True
    assert defaults["user_profile.relationship_enabled"] is True
    assert defaults["user_profile.injection_max_chars"] == 800
    assert defaults["user_profile.relationship_reserved_chars"] == 350
    assert defaults["user_profile.fact_maintenance_context_limit"] == 200
    assert defaults["user_profile.contract_correction_retries"] == 2
    assert defaults["user_profile.lifecycle_scan_interval_hours"] == 24
    assert defaults["user_profile.stale_retention_days"] == 180
    assert defaults["user_profile.relationship_rebuild_batch_limit"] == 32
    assert defaults["user_profile.fact_min_profile_value"] == 0.65
    assert defaults["user_profile.behavior_evidence_pool_limit"] == 128
    assert defaults["user_profile.undated_plan_review_days"] == 30
    assert defaults["user_profile.dated_plan_grace_days"] == 3
    assert defaults["user_profile.relationship_behavior_mode"] == "natural"
    assert set(defaults) == set(USER_PROFILE_SETTING_DEFINITIONS)
    assert all(
        definition["category"] == "user_profile"
        and definition.get("group")
        and definition.get("description")
        for definition in USER_PROFILE_SETTING_DEFINITIONS.values()
    )


def test_user_profile_settings_validate_scalar_and_cross_field_rules():
    effective = effective_user_profile_settings(
        {
            "user_profile.provider_id": " profile-provider ",
            "user_profile.relationship_behavior_mode": "unrestricted",
        }
    )
    assert effective["user_profile.provider_id"] == "profile-provider"
    assert effective["user_profile.relationship_behavior_mode"] == "unrestricted"

    with pytest.raises(ValueError, match="must be <="):
        validate_user_profile_setting("user_profile.maintenance_concurrency", 17)
    with pytest.raises(ValueError, match="unsupported"):
        validate_user_profile_setting(
            "user_profile.relationship_behavior_mode", "unsafe"
        )
    with pytest.raises(ValueError, match="预留字符"):
        effective_user_profile_settings(
            {
                "user_profile.injection_max_chars": 300,
                "user_profile.relationship_reserved_chars": 301,
            }
        )
    with pytest.raises(ValueError, match="敏感行为推断"):
        effective_user_profile_settings(
            {
                "user_profile.behavior_inference_min_confidence": 0.9,
                "user_profile.sensitive_inference_min_confidence": 0.8,
            }
        )
