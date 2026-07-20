from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_livingmemory.core.models.identity_profile import (
    AuthoritativeIdentityStore,
)
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
    store = AuthoritativeIdentityStore(path)

    store.replace_profiles([_profile()])
    reloaded = AuthoritativeIdentityStore(path)

    assert reloaded.load_error == ""
    assert reloaded.profiles[0].user_id == "1141337347"
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


@pytest.mark.asyncio
async def test_identity_handler_saves_profiles_and_blocks_during_topic_build(
    tmp_path,
) -> None:
    store = AuthoritativeIdentityStore(tmp_path / "authoritative_identities.json")
    handler = IdentityHandler(PageApiUtils())
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"profiles": [_profile()]})

    with patch(
        "astrbot_plugin_livingmemory.core.page_api_modules.identity_handler.request",
        request,
    ):
        blocked = await handler.save_profiles(store, topic_build_active=True)
        saved = await handler.save_profiles(store, topic_build_active=False)

    assert blocked["status"] == "error"
    assert store.profiles[0].display_name == "空雨"
    assert saved["status"] == "ok"
    assert saved["data"]["profiles"] == [_profile()]
