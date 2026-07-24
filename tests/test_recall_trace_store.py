from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.storage.recall_trace_store import RecallTraceStore


@pytest.mark.asyncio
async def test_recall_trace_store_defaults_off_and_round_trips_records(tmp_path):
    store = RecallTraceStore(str(tmp_path / "traces.db"), max_records_per_type=20)
    await store.initialize()

    assert await store.production_enabled() is False
    assert await store.set_production_enabled(True) is True
    uid = await store.record(
        trace_type="production",
        status="injected",
        query_text="六月工资",
        session_id="qq:FriendMessage:1",
        result_count=1,
        request_data={"top_k": 5},
        result_data={"items": [{"content": "工资补发"}]},
        diagnostics={"selected": 1},
        injection={"content": "exact injected text"},
    )

    listed = await store.list_records("production")
    assert [item["trace_uid"] for item in listed] == [uid]
    assert "result_json" not in listed[0]
    detail = await store.get_record(uid)
    assert detail is not None
    assert detail["injection"]["content"] == "exact injected text"
    assert detail["result"]["items"][0]["content"] == "工资补发"

    assert await store.delete_record(uid, trace_type="production") is True
    assert await store.get_record(uid) is None


@pytest.mark.asyncio
async def test_recall_trace_store_clear_is_scoped_by_type(tmp_path):
    store = RecallTraceStore(str(tmp_path / "traces.db"))
    await store.initialize()
    await store.record(trace_type="production", status="no_match", query_text="a")
    await store.record(trace_type="test", status="completed", query_text="b")

    assert await store.clear_records("test") == 1
    assert len(await store.list_records("production")) == 1
    assert await store.list_records("test") == []
