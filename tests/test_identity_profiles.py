from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_livingmemory.core.models.identity_profile import (
    AuthoritativeIdentityStore,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import stable_actor_id
from astrbot_plugin_livingmemory.core.models.platform_identity import canonical_platform
from astrbot_plugin_livingmemory.core.page_api_modules.identity_handler import (
    IdentityHandler,
)
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils


def _profile(user_id: str = "10000001") -> dict:
    return {
        "platform": "qq",
        "user_id": user_id,
        "display_name": "示例甲",
        "gender": "男性",
        "pronouns": ["他", "他的"],
    }


def test_identity_store_persists_and_reloads_atomically(tmp_path) -> None:
    path = tmp_path / "authoritative_identities.json"
    store = AuthoritativeIdentityStore(path)

    store.replace_profiles([_profile()])
    reloaded = AuthoritativeIdentityStore(path)

    assert reloaded.load_error == ""
    assert reloaded.profiles[0].user_id == "10000001"
    assert reloaded.profiles[0].pronouns == ("他", "他的")
    assert reloaded.payload()["profiles"] == [_profile()]


def test_identity_store_rejects_duplicates_without_changing_current_data(
    tmp_path,
) -> None:
    path = tmp_path / "authoritative_identities.json"
    store = AuthoritativeIdentityStore(path)
    store.replace_profiles([_profile()])
    previous_text = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        store.replace_profiles([_profile(), _profile()])

    assert path.read_text(encoding="utf-8") == previous_text
    assert len(store.profiles) == 1


def test_identity_store_rejects_overlapping_platform_aliases() -> None:
    store = AuthoritativeIdentityStore()

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
    store = AuthoritativeIdentityStore(tmp_path / "authoritative_identities.json")
    store.replace_profiles(
        [
            {
                **_profile(),
                "platform": "aiocqhttp",
                "platform_instances": ["QQ20000001"],
            }
        ]
    )

    payload = AuthoritativeIdentityStore(store.path).payload()["profiles"][0]
    assert payload["platform"] == "qq"
    assert payload["platform_aliases"] == ["aiocqhttp"]
    assert payload["platform_instances"] == ["QQ20000001"]


@pytest.mark.asyncio
async def test_identity_handler_combines_runtime_history_and_profile_platforms() -> None:
    store = AuthoritativeIdentityStore(
        profiles=[{**_profile(), "platform": "qq_official"}]
    )
    runtime = SimpleNamespace(
        meta=lambda: SimpleNamespace(
            name="aiocqhttp",
            id="QQ20000001",
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
    assert option["instance_ids"] == ["QQ20000001"]
    assert {"runtime", "history", "profile"} <= set(option["sources"])
    assert response["data"]["profiles"][0]["platform"] == "qq"


@pytest.mark.asyncio
async def test_identity_handler_saves_profiles_and_blocks_during_topic_build(
    tmp_path,
) -> None:
    store = AuthoritativeIdentityStore(tmp_path / "authoritative_identities.json")
    handler = IdentityHandler(PageApiUtils())
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"profiles": [_profile()]})
    on_saved = AsyncMock(
        return_value={
            "affected_spaces": 1,
            "affected_timelines": 2,
            "queued": True,
        }
    )

    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        blocked = await handler.save_profiles(store, topic_build_active=True)
        saved = await handler.save_profiles(
            store,
            topic_build_active=False,
            on_saved=on_saved,
        )

    assert blocked["status"] == "error"
    assert store.profiles[0].display_name == "示例甲"
    assert saved["status"] == "ok"
    assert saved["data"]["profiles"] == [_profile()]
    assert saved["data"]["topic_sync"]["queued"] is True
    on_saved.assert_awaited_once_with([], [_profile()], sync_mode="queue")


@pytest.mark.asyncio
async def test_identity_deletion_requires_impact_confirmation_and_forwards_sync_mode(
    tmp_path,
) -> None:
    store = AuthoritativeIdentityStore(tmp_path / "authoritative_identities.json")
    store.replace_profiles([_profile()])
    handler = IdentityHandler(PageApiUtils())
    impact_resolver = AsyncMock(
        return_value={
            "topic_count": 2,
            "timeline_uids": ["timeline-1"],
        }
    )
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"profiles": []})

    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        preview = await handler.preview_profile_changes(
            store,
            impact_resolver=impact_resolver,
        )
        rejected = await handler.save_profiles(store)

    assert preview["status"] == "ok"
    assert preview["data"]["topic_count"] == 2
    assert rejected["status"] == "error"
    assert store.payload()["profiles"] == [_profile()]
    impact_resolver.assert_awaited_once_with([_profile()], [])

    async def on_saved(previous, current, *, sync_mode):
        return {
            "previous": previous,
            "current": current,
            "sync_mode": sync_mode,
            "queued": True,
        }

    request.get_json = AsyncMock(
        return_value={
            "profiles": [],
            "confirm_identity_deletions": True,
            "sync_mode": "immediate",
        }
    )
    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        saved = await handler.save_profiles(store, on_saved=on_saved)

    assert saved["status"] == "ok"
    assert saved["data"]["profiles"] == []
    assert saved["data"]["topic_sync"]["sync_mode"] == "immediate"
