import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine


@pytest.mark.asyncio
async def test_session_migration_is_checked_once_for_concurrent_messages():
    engine = object.__new__(MemoryEngine)
    engine._session_migration_lock = asyncio.Lock()
    engine._session_migration_checked = set()
    engine._migrate_session_data_locked = AsyncMock(return_value=True)

    await asyncio.gather(
        *[
            MemoryEngine._migrate_session_data_if_needed(
                engine, "qq:FriendMessage:user-1"
            )
            for _ in range(3)
        ]
    )

    engine._migrate_session_data_locked.assert_awaited_once_with(
        "qq:FriendMessage:user-1"
    )
