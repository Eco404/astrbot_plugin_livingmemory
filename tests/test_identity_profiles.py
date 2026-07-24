from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_livingmemory.core.models.identity_profile import (
    SupplementalIdentityProfile,
    SupplementalIdentityStore,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import stable_actor_id
from astrbot_plugin_livingmemory.core.models.platform_identity import canonical_platform
from astrbot_plugin_livingmemory.core.page_api_modules.identity_handler import (
    IdentityHandler,
)
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils


def _profile(user_id: str = "1141337347") -> dict:
    return {
        "platform": "qq",
        "user_id": user_id,
        "display_name": "空雨",
        "gender": "男性",
        "pronouns": ["他", "他的"],
    }


def test_identity_store_persists_and_reloads_atomically(tmp_path) -> None:
    path = tmp_path / "authoritative_identities.json"
    store = SupplementalIdentityStore(path)

    store.replace_profiles([_profile()])
    reloaded = SupplementalIdentityStore(path)

    assert reloaded.load_error == ""
    assert reloaded.profiles[0].user_id == "1141337347"
    assert reloaded.profiles[0].pronouns == ("他", "他的")
    assert reloaded.payload()["profiles"] == [_profile()]


def test_identity_store_rejects_duplicates_without_changing_current_data(
    tmp_path,
) -> None:
    path = tmp_path / "authoritative_identities.json"
    store = SupplementalIdentityStore(path)
    store.replace_profiles([_profile()])
    previous_text = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        store.replace_profiles([_profile(), _profile()])

    assert path.read_text(encoding="utf-8") == previous_text
    assert len(store.profiles) == 1


def test_identity_store_rejects_overlapping_platform_aliases() -> None:
    store = SupplementalIdentityStore()

    with pytest.raises(ValueError, match="overlapping"):
        store.replace_profiles(
            [
                _profile(),
                {**_profile(), "platform": "qq_official"},
            ]
        )


def test_platform_aliases_share_one_canonical_actor_identity() -> None:
    assert canonical_platform("aiocqhttp") == "qq"
    assert canonical_platform("qq_official_webhook") == "qq"
    assert stable_actor_id("aiocqhttp", "42", "human") == "qq:human:42"
    assert stable_actor_id("custom_adapter", "42", "human") == (
        "custom_adapter:human:42"
    )


def test_identity_store_preserves_selected_platform_provenance(tmp_path) -> None:
    store = SupplementalIdentityStore(tmp_path / "authoritative_identities.json")
    store.replace_profiles(
        [
            {
                **_profile(),
                "platform": "aiocqhttp",
                "platform_instances": ["QQ2949374079"],
            }
        ]
    )

    payload = SupplementalIdentityStore(store.path).payload()["profiles"][0]
    assert payload["platform"] == "qq"
    assert payload["platform_aliases"] == ["aiocqhttp"]
    assert payload["platform_instances"] == ["QQ2949374079"]


@pytest.mark.asyncio
async def test_identity_handler_combines_runtime_history_and_profile_platforms() -> None:
    store = SupplementalIdentityStore(
        profiles=[{**_profile(), "platform": "qq_official"}]
    )
    runtime = SimpleNamespace(
        meta=lambda: SimpleNamespace(
            name="aiocqhttp",
            id="QQ2949374079",
            adapter_display_name="QQ",
        )
    )
    platform_manager = SimpleNamespace(get_insts=lambda: [runtime])
    conversation_manager = SimpleNamespace(
        store=SimpleNamespace(
            get_recent_sessions=AsyncMock(
                return_value=[SimpleNamespace(platform="aiocqhttp")]
            )
        )
    )

    response = await IdentityHandler(PageApiUtils()).list_profiles(
        store,
        platform_manager=platform_manager,
        conversation_manager=conversation_manager,
    )

    option = response["data"]["platform_options"][0]
    assert option["value"] == "qq"
    assert option["instance_ids"] == ["QQ2949374079"]
    assert {"runtime", "history", "profile"} <= set(option["sources"])
    assert response["data"]["profiles"][0]["platform"] == "qq"


@pytest.mark.asyncio
async def test_identity_handler_saves_profiles_without_topic_rebuild(
    tmp_path,
) -> None:
    store = SupplementalIdentityStore(tmp_path / "authoritative_identities.json")
    handler = IdentityHandler(PageApiUtils())
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"profiles": [_profile()]})
    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        saved = await handler.save_profiles(store)

    assert store.profiles[0].display_name == "空雨"
    assert saved["status"] == "ok"
    assert saved["data"]["profiles"] == [_profile()]
    assert saved["data"]["applies_to"] == "future_generation_or_rebuild"
    assert "topic_sync" not in saved["data"]


@pytest.mark.asyncio
async def test_identity_deletion_does_not_require_impact_or_trigger_rebuild(
    tmp_path,
) -> None:
    store = SupplementalIdentityStore(tmp_path / "authoritative_identities.json")
    store.replace_profiles([_profile()])
    handler = IdentityHandler(PageApiUtils())
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"profiles": []})

    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        saved = await handler.save_profiles(store)

    assert saved["status"] == "ok"
    assert saved["data"]["profiles"] == []
    assert saved["data"]["applies_to"] == "future_generation_or_rebuild"


def test_supplemental_profile_matches_only_stable_actor_identity() -> None:
    profile = SupplementalIdentityProfile.from_mapping(_profile())

    assert profile.matches_actor_id("qq:human:1141337347") is True
    assert profile.matches_actor_id("aiocqhttp:human:1141337347") is True
    assert profile.matches_actor_id("qq:human:different-account") is False
    assert profile.matches_actor_id("unresolved:fragment:空雨") is False
    assert profile.matches_message(
        sender_id="different-account",
        sender_name="空雨",
        platform="qq",
    ) is False
