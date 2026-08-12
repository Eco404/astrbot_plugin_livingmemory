"""
Tests for PluginPageApi — WebUI REST API endpoints and helpers.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from astrbot_plugin_livingmemory.core.models.identity_profile import (
    SupplementalIdentityStore,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import TopicMemory
from astrbot_plugin_livingmemory.core.page_api import (
    PAGE_API_PREFIX,
    PLUGIN_NAME,
    PluginPageApi,
)

# ---------------------------------------------------------------------------
# Fake / stub helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeMemoryEngine:
    db_path: str = ":memory:"
    graph_store: Any = None
    stats: dict = field(
        default_factory=lambda: {
            "total_memories": 42,
            "status_breakdown": {"active": 30, "archived": 8, "deleted": 4},
            "sessions": {"s1": 10, "s2": 5},
        }
    )

    async def get_statistics(self):
        return self.stats

    async def search_memories(
        self,
        query,
        k=5,
        session_id=None,
        persona_id=None,
        track_access=True,
    ):
        return []

    async def get_memory(self, memory_id: int):
        return None

    async def add_memory(self, **kwargs):
        return 999

    async def delete_memory(self, memory_id: int):
        return True

    async def update_memory(self, memory_id: int, updates: dict):
        return True

    async def replace_memory(self, memory_id: int, **kwargs):
        return 999

    async def rewrite_memory_in_place(self, memory_id: int, **kwargs):
        return memory_id

    async def batch_delete_memories(self, memory_ids: list[int]):
        return len(memory_ids)

    async def get_memory_transfer_records(self, memory_ids=None):
        records = [
            {
                "original_id": 1,
                "content": "portable memory",
                "importance": 0.7,
                "session_id": "s1",
                "persona_id": "p1",
                "metadata": {"topics": ["portable"]},
                "source_messages": [],
            }
        ]
        if memory_ids is None or 1 in memory_ids:
            return records
        return []

    async def get_memory_import_keys(self):
        return {("existing memory", "s1", "p1")}

    async def close(self):
        pass


class FakeInitializer:
    def __init__(self):
        self.memory_engine = FakeMemoryEngine()
        self.conversation_manager = None
        self.memory_processor = SimpleNamespace()
        self.index_validator = None
        self.config_manager = MagicMock()
        self.config_manager.get.side_effect = lambda _key, default=None: default
        self.llm_provider = None
        self.embedding_provider = None
        self.rerank_provider = None
        self.identity_profile_store = SupplementalIdentityStore()
        self.data_dir = "/tmp/test_plugin"


class FakePlugin:
    def __init__(self, *, ready=True, memory_engine=None):
        self._ready = ready
        self._fail_message = "" if ready else "插件尚未就绪"
        self.initializer = FakeInitializer()
        if memory_engine:
            self.initializer.memory_engine = memory_engine
        self._api_routes = []

    async def _ensure_plugin_ready(self):
        return self._ready, self._fail_message

    @property
    def context(self):
        ctx = MagicMock()

        def _register(route, handler, methods, desc):
            self._api_routes.append((route, handler, methods, desc))

        ctx.register_web_api = _register
        return ctx


# ---------------------------------------------------------------------------
# Safe request mocking — Quart's request is a LocalProxy that throws
# RuntimeError when accessed outside a request context. Use patch.dict
# on the module's __dict__ to replace it without triggering the proxy.
# ---------------------------------------------------------------------------


def _mock_page_request(**overrides):
    """Build a MagicMock suitable for standing in as ``quart.request``.

    *overrides* can include ``args`` (dict), ``get_json`` (return value),
    and ``method`` (str).  All keys are optional.
    """
    req = MagicMock()

    args_mock = MagicMock()
    args_dict = overrides.get("args", {})
    args_mock.get.side_effect = lambda key, default=None: args_dict.get(key, default)
    req.args = args_mock

    json_value = overrides.get("get_json", {})
    req.get_json = AsyncMock(return_value=json_value)

    req.method = overrides.get("method", "GET")
    return req


@contextmanager
def _patch_page_request(req: MagicMock):
    """Temporarily replace ``page_api.request`` with *req*."""
    import astrbot_plugin_livingmemory.core.page_api as mod
    import astrbot_plugin_livingmemory.core.page_api_modules.graph_handler as graph_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.memory_handler as memory_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.recall_handler as recall_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.session_handler as session_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.settings_handler as settings_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.timeline_handler as timeline_mod
    import astrbot_plugin_livingmemory.core.page_api_modules.topic_handler as topic_mod

    # Patch all modules that use request
    modules = [
        mod,
        memory_mod,
        recall_mod,
        graph_mod,
        session_mod,
        settings_mod,
        timeline_mod,
        topic_mod,
    ]
    old_values = []

    for module in modules:
        ns = vars(module)
        old_values.append((ns, ns.get("request")))
        ns["request"] = req

    try:
        yield
    finally:
        for ns, old in old_values:
            if old is not None:
                ns["request"] = old
            else:
                ns.pop("request", None)


# Alias for brevity in tests
def _qp(req=None, **kw):
    """Quick-patch: ``with _qp(mock_req): ...``"""
    if req is None:
        req = _mock_page_request(**kw)
    return _patch_page_request(req)


# ---------------------------------------------------------------------------
# Helper method unit tests (no plugin needed)
# ---------------------------------------------------------------------------


class TestResponseHelpers:
    def test_ok_returns_status_format(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        result = utils.ok({"items": [1, 2]})
        assert result == {"status": "ok", "data": {"items": [1, 2]}}

    def test_ok_defaults_to_none_data(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        result = utils.ok()
        assert result == {"status": "ok", "data": None}

    def test_error_returns_status_format(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        result = utils.error("something went wrong")
        assert result == {"status": "error", "message": "something went wrong"}

    def test_error_converts_non_string(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        result = utils.error(ValueError("boom"))
        assert result["status"] == "error"
        assert "boom" in result["message"]


class TestNumberHelpers:
    def test_importance_to_display_handles_non_numeric_values(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        assert utils.importance_to_display("default") == 5.0


class TestOptionalText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("None", None),
            ("null", None),
            ("undefined", None),
            (" s1 ", "s1"),
        ],
    )
    def test_optional_filter_values(self, raw, expected):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        assert utils.optional_text(raw) == expected


class TestSessionCatalogHelpers:
    def test_parse_group_session(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import SessionHandler

        parsed = SessionHandler._parse_session_id(
            "bot-account:GroupMessage:group-123"
        )
        assert parsed == {
            "platform_id": "bot-account",
            "message_type": "GroupMessage",
            "chat_type": "group",
            "target_id": "group-123",
        }

    def test_parse_private_session_preserves_complex_target(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import SessionHandler

        parsed = SessionHandler._parse_session_id(
            "bot-account:FriendMessage:webchat!astrbot!user:42"
        )
        assert parsed["chat_type"] == "private"
        assert parsed["target_id"] == "webchat!astrbot!user:42"


class TestNormalizeMetadata:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ({"key": "value"}, {"key": "value"}),
            (None, {}),
            ("", {}),
            ('{"a":1}', {"a": 1}),
            ("not-json", {}),
            (123, {}),
            ([1, 2, 3], {}),  # valid JSON but not a dict
        ],
    )
    def test_normalize_metadata(self, raw, expected):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        assert utils.normalize_metadata(raw) == expected


class TestTokenizeGraphQuery:
    def test_empty_query(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        assert utils.tokenize_graph_query("") == []
        assert utils.tokenize_graph_query("   ") == []

    def test_english_query(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        tokens = utils.tokenize_graph_query("machine learning")
        assert "machine" in tokens
        assert "learning" in tokens

    def test_chinese_query(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        tokens = utils.tokenize_graph_query("人工智能发展")
        assert len(tokens) >= 1

    def test_short_tokens_filtered(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        tokens = utils.tokenize_graph_query("a b c d")
        assert all(len(t) >= 2 for t in tokens)

    def test_caps_returns_at_most_12(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        tokens = utils.tokenize_graph_query(
            "a b c d e f g h i j k l m n o p q r s t u v w x y z"
        )
        assert len(tokens) <= 12

    def test_mixed_chinese_english(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        tokens = utils.tokenize_graph_query("AI and 机器学习")
        assert len(tokens) >= 1


class TestBuildGraphViewPayload:
    def test_basic_structure(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        snapshot = {"nodes": [], "edges": [], "entries": [], "memories": []}
        stats = {"graph_nodes": 0, "graph_edges": 0, "graph_entries": 0}
        result = utils.build_graph_view_payload(
            snapshot,
            stats,
            enabled=True,
            mode="overview",
            filters={},
        )
        assert result["enabled"] is True
        assert result["mode"] == "overview"
        assert "summary" in result
        assert "snapshot" in result
        assert "retrieval" in result

    def test_nodes_get_highlighted(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        snapshot = {
            "nodes": [
                {"id": 1, "type": "topic", "weight": 0.8, "degree": 3, "label": "AI"}
            ],
            "edges": [],
            "entries": [],
            "memories": [],
        }
        stats = {"graph_nodes": 1, "graph_edges": 0, "graph_entries": 0}
        result = utils.build_graph_view_payload(
            snapshot,
            stats,
            enabled=True,
            mode="query",
            matched_node_ids=[1],
            filters={},
        )
        assert result["snapshot"]["nodes"][0]["highlighted"] is True

    def test_top_nodes_sorted_by_weight(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        snapshot = {
            "nodes": [
                {"id": 1, "type": "topic", "weight": 0.3, "degree": 1, "label": "B"},
                {"id": 2, "type": "topic", "weight": 0.9, "degree": 5, "label": "A"},
            ],
            "edges": [],
            "entries": [],
            "memories": [],
        }
        stats = {"graph_nodes": 2, "graph_edges": 0, "graph_entries": 0}
        result = utils.build_graph_view_payload(
            snapshot,
            stats,
            enabled=True,
            mode="overview",
            filters={},
        )
        top = result["top_nodes"]
        assert top[0]["id"] == 2  # higher weight first

    def test_node_type_breakdown(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        snapshot = {
            "nodes": [
                {"id": 1, "type": "topic"},
                {"id": 2, "type": "topic"},
                {"id": 3, "type": "person"},
            ],
            "edges": [],
            "entries": [],
            "memories": [],
        }
        stats = {"graph_nodes": 3, "graph_edges": 0, "graph_entries": 0}
        result = utils.build_graph_view_payload(
            snapshot,
            stats,
            enabled=True,
            mode="overview",
            filters={},
        )
        breakdown = result["summary"]["node_type_breakdown"]
        assert breakdown.get("topic") == 2
        assert breakdown.get("person") == 1

    def test_non_numeric_weights_and_importance_do_not_break_sorting(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        snapshot = {
            "nodes": [
                {"id": 1, "type": "topic", "weight": "auto", "degree": 1},
                {"id": 2, "type": "topic", "weight": 0.9, "degree": 0},
            ],
            "edges": [],
            "entries": [],
            "memories": [
                {"memory_id": 1, "importance": "default", "entry_count": 1},
                {"memory_id": 2, "importance": 0.8, "entry_count": 1},
            ],
        }
        stats = {"graph_nodes": 2, "graph_edges": 0, "graph_entries": 0}

        result = utils.build_graph_view_payload(
            snapshot,
            stats,
            enabled=True,
            mode="overview",
            filters={},
        )

        assert result["top_nodes"][0]["id"] == 2
        assert result["top_memories"][0]["memory_id"] == 2


class TestGetGraphStore:
    def test_returns_graph_store_attribute(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        engine = FakeMemoryEngine()
        engine.graph_store = object()
        assert utils.get_graph_store(engine) is engine.graph_store

    def test_returns_none_when_no_graph_store(self):
        from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils

        utils = PageApiUtils()
        engine = FakeMemoryEngine()
        engine.graph_store = None
        assert utils.get_graph_store(engine) is None


# ---------------------------------------------------------------------------
# Endpoint tests (with mocked plugin)
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    plugin = FakePlugin()
    return PluginPageApi(plugin)


@pytest.fixture
def api_not_ready():
    plugin = FakePlugin(ready=False)
    return PluginPageApi(plugin)


class TestGetStats:
    @pytest.mark.asyncio
    async def test_returns_stats(self, api):
        result = await api.get_stats()
        assert result["status"] == "ok"
        assert result["data"]["total_memories"] == 42
        assert result["data"]["status_breakdown"]["active"] == 30

    @pytest.mark.asyncio
    async def test_plugin_not_ready(self, api_not_ready):
        result = await api_not_ready.get_stats()
        assert result["status"] == "error"
        assert "尚未就绪" in result["message"]


class TestTopicList:
    @pytest.mark.asyncio
    async def test_defaults_to_active_and_reports_unfiltered_space_total(self, api):
        engine = api.plugin.initializer.memory_engine

        async def count_topics(
            _space_id,
            *,
            status=None,
            actor_id=None,
            search_text=None,
        ):
            assert actor_id is None
            assert search_text is None
            return 0 if status == "active" else 4

        engine.topic_memory_store = SimpleNamespace(
            list_topics=AsyncMock(return_value=[]),
            count_topics=AsyncMock(side_effect=count_topics),
            list_topic_actors=AsyncMock(return_value=[]),
        )
        req = _mock_page_request(args={"memory_space_id": "space-1"})

        with _patch_page_request(req):
            result = await api.list_topics()

        assert result["status"] == "ok"
        assert result["data"]["status_filter"] == "active"
        assert result["data"]["total"] == 0
        assert result["data"]["space_total"] == 4
        engine.topic_memory_store.list_topics.assert_awaited_once_with(
            "space-1",
            status="active",
            limit=100,
            offset=0,
            actor_id=None,
            search_text=None,
        )

    @pytest.mark.asyncio
    async def test_error_status_filter_does_not_collide_with_response_status(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.topic_memory_store = SimpleNamespace(
            list_topics=AsyncMock(return_value=[]),
            count_topics=AsyncMock(return_value=0),
            list_topic_actors=AsyncMock(return_value=[]),
        )
        req = _mock_page_request(
            args={"memory_space_id": "space-1", "status": "error"}
        )

        with _patch_page_request(req):
            result = await api.list_topics()

        assert result["status"] == "ok"
        assert result["data"]["status_filter"] == "error"
        assert "status" not in result["data"]
        engine.topic_memory_store.list_topics.assert_awaited_once_with(
            "space-1",
            status="error",
            limit=100,
            offset=0,
            actor_id=None,
            search_text=None,
        )


    @pytest.mark.asyncio
    async def test_keyword_search_is_forwarded_to_store(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.topic_memory_store = SimpleNamespace(
            list_topics=AsyncMock(return_value=[]),
            count_topics=AsyncMock(side_effect=[2, 7]),
            list_topic_actors=AsyncMock(return_value=[]),
        )
        req = _mock_page_request(
            args={
                "memory_space_id": "space-1",
                "search_query": "项目报销",
                "search_mode": "keyword",
            }
        )

        with _patch_page_request(req):
            result = await api.list_topics()

        assert result["status"] == "ok"
        assert result["data"]["total"] == 2
        assert result["data"]["space_total"] == 7
        engine.topic_memory_store.list_topics.assert_awaited_once_with(
            "space-1",
            status="active",
            limit=100,
            offset=0,
            actor_id=None,
            search_text="项目报销",
        )

    @pytest.mark.asyncio
    async def test_semantic_search_uses_vector_order_and_actor_filter(self, api):
        engine = api.plugin.initializer.memory_engine
        topics = [
            TopicMemory(
                topic_uid="topic-b",
                memory_space_id="space-1",
                title="第二候选",
                summary="摘要 B",
            ),
            TopicMemory(
                topic_uid="topic-a",
                memory_space_id="space-1",
                title="第一候选",
                summary="摘要 A",
            ),
        ]
        store = SimpleNamespace(
            get_topics_by_uids=AsyncMock(return_value=topics),
            get_topic_support_metrics=AsyncMock(return_value={}),
            count_topics=AsyncMock(return_value=9),
            list_topic_actors=AsyncMock(return_value=[]),
        )
        provider = object()
        retriever = SimpleNamespace(
            embedding_provider=provider,
            refresh_providers=MagicMock(),
            _get_embeddings=AsyncMock(return_value=[[1.0, 0.0]]),
        )
        vector_index = SimpleNamespace(
            search=AsyncMock(
                return_value=[
                    SimpleNamespace(artifact_uid="topic-a", score=0.91),
                    SimpleNamespace(artifact_uid="topic-b", score=0.72),
                ]
            )
        )
        engine.topic_memory_store = store
        engine.topic_retriever = retriever
        engine.topic_vector_index = vector_index
        req = _mock_page_request(
            args={
                "memory_space_id": "space-1",
                "actor_id": "qq:human:10000001",
                "search_query": "报销差额",
                "search_mode": "semantic",
            }
        )

        with _patch_page_request(req):
            result = await api.list_topics()

        assert result["status"] == "ok"
        assert [item["topic_uid"] for item in result["data"]["items"]] == [
            "topic-a",
            "topic-b",
        ]
        assert result["data"]["items"][0]["search_score"] == pytest.approx(0.91)
        store.get_topics_by_uids.assert_awaited_once_with(
            "space-1",
            ["topic-a", "topic-b"],
            status="active",
            actor_id="qq:human:10000001",
        )
        vector_index.search.assert_awaited_once()


class TestSessionCatalog:
    @pytest.mark.asyncio
    async def test_layered_filters(self, api):
        sessions = [
            SimpleNamespace(
                session_id="bot-a:GroupMessage:group-1",
                platform="qq",
                created_at=10.0,
                last_active_at=200.0,
                message_count=12,
            ),
            SimpleNamespace(
                session_id="bot-a:FriendMessage:user-1",
                platform="qq",
                created_at=20.0,
                last_active_at=300.0,
                message_count=8,
            ),
            SimpleNamespace(
                session_id="bot-b:GroupMessage:group-2",
                platform="qq",
                created_at=30.0,
                last_active_at=400.0,
                message_count=6,
            ),
        ]
        store = SimpleNamespace(get_recent_sessions=AsyncMock(return_value=sessions))
        api.plugin.initializer.conversation_manager = SimpleNamespace(store=store)
        req = _mock_page_request(
            args={
                "platform_id": "bot-a",
                "chat_type": "group",
                "updated_after": "100",
                "target_query": "group",
            }
        )

        with _patch_page_request(req):
            result = await api.list_sessions()

        assert result["status"] == "ok"
        assert result["data"]["facets"]["platform_ids"] == ["bot-a", "bot-b"]
        assert [item["session_id"] for item in result["data"]["items"]] == [
            "bot-a:GroupMessage:group-1"
        ]

    @pytest.mark.asyncio
    async def test_target_items_wait_for_required_layers(self, api):
        sessions = [
            SimpleNamespace(
                session_id="bot-a:GroupMessage:group-1",
                platform="qq",
                created_at=10.0,
                last_active_at=200.0,
                message_count=12,
            )
        ]
        store = SimpleNamespace(get_recent_sessions=AsyncMock(return_value=sessions))
        api.plugin.initializer.conversation_manager = SimpleNamespace(store=store)
        req = _mock_page_request(args={})

        with _patch_page_request(req):
            result = await api.list_sessions()

        assert result["status"] == "ok"
        assert result["data"]["facets"]["platform_ids"] == ["bot-a"]
        assert result["data"]["items"] == []

    @pytest.mark.asyncio
    async def test_flat_catalog_returns_sessions_without_layer_selection(self, api):
        sessions = [
            SimpleNamespace(
                session_id="bot-a:FriendMessage:user-1",
                platform="qq",
                created_at=10.0,
                last_active_at=200.0,
                message_count=3,
            )
        ]
        store = SimpleNamespace(get_recent_sessions=AsyncMock(return_value=sessions))
        api.plugin.initializer.conversation_manager = SimpleNamespace(store=store)
        req = _mock_page_request(args={"flat": "true", "target_query": "user-1"})
        with _patch_page_request(req):
            result = await api.list_sessions()
        assert result["status"] == "ok"
        assert result["data"]["items"][0]["session_id"] == sessions[0].session_id


class TestListMemories:
    @pytest.mark.asyncio
    async def test_missing_db_path(self, api):
        api.plugin.initializer.memory_engine.db_path = None
        req = _mock_page_request(
            args={
                "page": "1",
                "page_size": "20",
                "session_id": "",
                "keyword": "",
                "status": "all",
            }
        )
        with _patch_page_request(req):
            result = await api.list_memories()
        assert result["status"] == "error"
        assert "db_path" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_pagination(self, api):
        req = _mock_page_request(
            args={
                "page": "not-a-number",
                "page_size": "20",
                "session_id": "",
                "keyword": "",
                "status": "all",
            }
        )
        with _patch_page_request(req):
            result = await api.list_memories()
        assert result["status"] == "error"
        assert "分页" in result["message"]

    @pytest.mark.asyncio
    async def test_valid_request(self, api):
        req = _mock_page_request(
            args={
                "page": "1",
                "page_size": "20",
                "session_id": "",
                "keyword": "",
                "status": "all",
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.aiosqlite"
            ) as mock_sqlite:
                mock_conn = AsyncMock()
                mock_conn.execute.return_value = mock_conn
                mock_conn.fetchone.return_value = {"total": 0}
                mock_conn.fetchall.return_value = []
                mock_sqlite.connect.return_value.__aenter__.return_value = mock_conn
                mock_sqlite.Row = dict

                result = await api.list_memories()
        assert result["status"] == "ok"
        assert result["data"]["total"] == 0
        assert result["data"]["items"] == []

    @pytest.mark.asyncio
    async def test_type_filter_and_sort_are_applied_in_sql(self, api, tmp_path):
        db_path = tmp_path / "memories.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    doc_id TEXT,
                    text TEXT,
                    metadata TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            rows = [
                (
                    1,
                    "1",
                    "low preference",
                    {"memory_type": "PREFERENCE", "importance": 0.3, "create_time": 10},
                ),
                (
                    2,
                    "2",
                    "high preference",
                    {"memory_type": "PREFERENCE", "importance": 0.9, "create_time": 20},
                ),
                (
                    3,
                    "3",
                    "other fact",
                    {"memory_type": "FACT", "importance": 1.0, "create_time": 30},
                ),
            ]
            for memory_id, doc_id, text, metadata in rows:
                await db.execute(
                    """
                    INSERT INTO documents
                        (id, doc_id, text, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        doc_id,
                        text,
                        json.dumps(metadata),
                        "created",
                        "updated",
                    ),
                )
            await db.commit()

        api.plugin.initializer.memory_engine.db_path = str(db_path)
        req = _mock_page_request(
            args={
                "page": "1",
                "page_size": "20",
                "session_id": "",
                "keyword": "",
                "status": "all",
                "type": "PREFERENCE",
                "sort": "importance_desc",
            }
        )

        with _patch_page_request(req):
            result = await api.list_memories()

        assert result["status"] == "ok"
        assert result["data"]["total"] == 2
        assert result["data"]["filters"]["type"] == "PREFERENCE"
        assert result["data"]["sort"] == "importance_desc"
        assert [item["id"] for item in result["data"]["items"]] == [2, 1]

    @pytest.mark.asyncio
    async def test_includes_batch_topic_counts(self, api, tmp_path):
        db_path = tmp_path / "memory-topic-counts.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    doc_id TEXT,
                    text TEXT,
                    metadata TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT INTO documents
                    (id, doc_id, text, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "1",
                    "Timeline content",
                    json.dumps({"memory_uid": "timeline-1"}),
                    "created",
                    "updated",
                ),
            )
            await db.commit()

        topic_store = SimpleNamespace(
            get_topic_counts_for_timelines=AsyncMock(
                return_value={"timeline-1": 3}
            )
        )
        engine = api.plugin.initializer.memory_engine
        engine.db_path = str(db_path)
        engine.topic_memory_store = topic_store
        req = _mock_page_request(args={"page": "1", "page_size": "20"})

        with _patch_page_request(req):
            result = await api.list_memories()

        assert result["status"] == "ok"
        assert result["data"]["items"][0]["topic_count"] == 3
        topic_store.get_topic_counts_for_timelines.assert_awaited_once_with(
            ["timeline-1"]
        )

    @pytest.mark.asyncio
    async def test_memory_detail_includes_only_active_related_topics(
        self, api, tmp_path
    ):
        db_path = tmp_path / "memory-topic-detail.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    doc_id TEXT,
                    text TEXT,
                    metadata TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT INTO documents
                    (id, doc_id, text, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    "7",
                    "Timeline detail",
                    json.dumps({"memory_uid": "timeline-7", "revision": 3}),
                    "created",
                    "updated",
                ),
            )
            await db.commit()

        topic_store = SimpleNamespace(
            get_topics_for_timeline=AsyncMock(
                return_value=[
                    {
                        "topic_uid": "topic-active",
                        "title": "Active Topic",
                        "summary": "Active summary",
                        "status": "active",
                        "link_status": "active",
                        "importance": 0.8,
                        "revision": 2,
                        "source_timeline_revision": 2,
                    },
                    {
                        "topic_uid": "topic-archived",
                        "title": "Archived Topic",
                        "status": "archived",
                        "link_status": "active",
                    },
                    {
                        "topic_uid": "topic-stale-link",
                        "title": "Stale link",
                        "status": "active",
                        "link_status": "archived",
                    },
                ]
            )
        )
        engine = api.plugin.initializer.memory_engine
        engine.db_path = str(db_path)
        engine.topic_memory_store = topic_store
        req = _mock_page_request(args={"memory_id": "7"})

        with _patch_page_request(req):
            result = await api.get_memory_detail()

        assert result["status"] == "ok"
        assert result["data"]["topic_count"] == 1
        assert [item["topic_uid"] for item in result["data"]["related_topics"]] == [
            "topic-active"
        ]
        assert result["data"]["related_topics"][0]["waiting_rebuild"] is True
        assert (
            result["data"]["related_topics"][0]["source_timeline_revision"] == 2
        )
        topic_store.get_topics_for_timeline.assert_awaited_once_with("timeline-7")

    @pytest.mark.asyncio
    async def test_plugin_not_ready(self, api_not_ready):
        req = _mock_page_request()
        with _patch_page_request(req):
            result = await api_not_ready.list_memories()
        assert result["status"] == "error"


class TestUpdateMemory:
    @pytest.mark.asyncio
    async def test_missing_memory_id(self, api):
        req = _mock_page_request(get_json={"field": "importance", "value": 0.8})
        with _patch_page_request(req):
            result = await api.update_memory()
        assert result["status"] == "error"
        assert "memory_id" in result["message"]

    @pytest.mark.asyncio
    async def test_memory_id_not_integer(self, api):
        req = _mock_page_request(
            get_json={"memory_id": "abc", "field": "importance", "value": 0.8}
        )
        with _patch_page_request(req):
            result = await api.update_memory()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_missing_field_or_value(self, api):
        req = _mock_page_request(get_json={"memory_id": 1})
        with _patch_page_request(req):
            result = await api.update_memory()
        assert result["status"] == "error"
        assert "field" in result["message"]

    @pytest.mark.asyncio
    async def test_unsupported_field(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "unsupported",
                "value": "x",
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()
        assert result["status"] == "error"
        assert "不支持" in result["message"]

    @pytest.mark.asyncio
    async def test_importance_out_of_range(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "importance",
                "value": 100,
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()
        assert result["status"] == "error"
        assert "重要性" in result["message"]

    @pytest.mark.asyncio
    async def test_importance_valid_range(self, api):
        api.plugin.initializer.memory_engine.update_memory = AsyncMock(return_value=True)
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "importance",
                "value": 8.5,
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()
        assert result["status"] == "ok"
        assert result["data"]["field"] == "importance"
        api.plugin.initializer.memory_engine.update_memory.assert_awaited_once()
        assert (
            api.plugin.initializer.memory_engine.update_memory.call_args.args[1][
                "importance"
            ]
            == 0.85
        )

    @pytest.mark.asyncio
    async def test_importance_display_scale_preserves_one_point_zero(self, api):
        api.plugin.initializer.memory_engine.update_memory = AsyncMock(return_value=True)
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "importance",
                "value": 1.0,
                "value_scale": "display",
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()

        assert result["status"] == "ok"
        updates = api.plugin.initializer.memory_engine.update_memory.call_args.args[1]
        assert updates["importance"] == 0.1

    @pytest.mark.asyncio
    async def test_importance_auto_scale_keeps_legacy_normalized_value(self, api):
        api.plugin.initializer.memory_engine.update_memory = AsyncMock(return_value=True)
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "importance",
                "value": 0.8,
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()

        assert result["status"] == "ok"
        updates = api.plugin.initializer.memory_engine.update_memory.call_args.args[1]
        assert updates["importance"] == 0.8

    @pytest.mark.asyncio
    async def test_status_invalid_value(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "status",
                "value": "invalid",
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value={"id": 1, "text": "hello", "metadata": {}},
            ):
                result = await api.update_memory()
        assert result["status"] == "error"
        assert "状态" in result["message"]

    @pytest.mark.asyncio
    async def test_memory_not_found(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 999,
                "field": "importance",
                "value": 0.5,
            }
        )
        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value=None,
            ):
                result = await api.update_memory()
        assert result["status"] == "error"
        assert "不存在" in result["message"]

    @pytest.mark.asyncio
    async def test_content_update_empty_value(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "content",
                "value": "   ",
            }
        )
        with _patch_page_request(req):
            memory = {
                "id": 1,
                "text": "hello",
                "metadata": {"session_id": "s1", "persona_id": "p1", "importance": 0.5},
            }
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value=memory,
            ):
                result = await api.update_memory()
        assert result["status"] == "error"
        assert "不能为空" in result["message"]

    @pytest.mark.asyncio
    async def test_content_update_uses_default_for_legacy_importance(self, api):
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "content",
                "value": "new content",
            }
        )
        with _patch_page_request(req):
            memory = {
                "id": 1,
                "text": "old content",
                "metadata": {
                    "session_id": "s1",
                    "persona_id": "p1",
                    "importance": "default",
                },
            }
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value=memory,
            ):
                processor = api.plugin.initializer.memory_processor
                processor.build_memory_from_structured_data = MagicMock(
                    return_value=(
                        "new content",
                        {
                            "canonical_summary": "new content",
                            "persona_summary": "new content",
                            "topics": [],
                            "key_facts": [],
                        },
                        0.5,
                    )
                )
                processor.classify_atoms_from_metadata = MagicMock(return_value=[])
                api.plugin.initializer.memory_engine.replace_memory = AsyncMock(
                    return_value=999
                )
                result = await api.update_memory()

        assert result["status"] == "ok"
        assert (
            api.plugin.initializer.memory_engine.replace_memory.call_args.kwargs[
                "importance"
            ]
            == 0.5
        )

    @pytest.mark.asyncio
    async def test_structured_update_rebuilds_atoms_and_derived_metadata(self, api):
        memory = {
            "id": 1,
            "text": "错误事实",
            "metadata": {
                "session_id": "bot-a:FriendMessage:user-1",
                "persona_id": "p1",
                "topics": ["旧主题"],
                "key_facts": ["旧事实"],
                "importance": 0.6,
            },
        }
        processor = api.plugin.initializer.memory_processor
        processor.build_memory_from_structured_data = MagicMock(
            return_value=(
                "正确摘要 | 正确事实",
                {
                    "canonical_summary": "正确摘要 | 正确事实",
                    "persona_summary": "正确摘要",
                    "topics": ["新主题"],
                    "key_facts": ["正确事实"],
                    "sentiment": "neutral",
                },
                0.8,
            )
        )
        atoms = [SimpleNamespace(content="正确事实")]
        processor.classify_atoms_from_metadata = MagicMock(return_value=atoms)
        api.plugin.initializer.memory_engine.replace_memory = AsyncMock(return_value=2)
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "field": "structured",
                "value": {
                    "summary": "正确摘要",
                    "topics": ["新主题"],
                    "key_facts": ["正确事实"],
                    "importance": 8,
                    "importance_scale": "display",
                },
                "reason": "纠错",
            }
        )

        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value=memory,
            ):
                result = await api.update_memory()

        assert result["status"] == "ok"
        assert result["data"]["new_memory_id"] == 2
        kwargs = api.plugin.initializer.memory_engine.replace_memory.call_args.kwargs
        assert kwargs["metadata"]["topics"] == ["新主题"]
        assert kwargs["metadata"]["key_facts"] == ["正确事实"]
        assert kwargs["metadata"]["participants"] == []
        assert kwargs["atoms"] == atoms

    @pytest.mark.asyncio
    async def test_structured_update_can_rebuild_in_place(self, api):
        memory = {
            "id": 7,
            "text": "旧内容",
            "metadata": {
                "session_id": "bot-a:FriendMessage:user-1",
                "persona_id": "p1",
                "importance": 0.5,
            },
        }
        processor = api.plugin.initializer.memory_processor
        processor.build_memory_from_structured_data = MagicMock(
            return_value=(
                "新内容",
                {
                    "canonical_summary": "新内容",
                    "persona_summary": "新内容",
                    "topics": ["新主题"],
                    "key_facts": ["新事实"],
                },
                0.7,
            )
        )
        processor.classify_atoms_from_metadata = MagicMock(return_value=[])
        api.plugin.initializer.memory_engine.rewrite_memory_in_place = AsyncMock(
            return_value=7
        )
        api.plugin.initializer.memory_engine.replace_memory = AsyncMock(return_value=999)
        req = _mock_page_request(
            get_json={
                "memory_id": 7,
                "field": "structured",
                "update_mode": "in_place",
                "value": {
                    "summary": "新内容",
                    "topics": ["新主题"],
                    "key_facts": ["新事实"],
                    "importance": 7,
                    "importance_scale": "display",
                },
            }
        )

        with _patch_page_request(req):
            with patch(
                "astrbot_plugin_livingmemory.core.page_api_modules.memory_handler.MemoryHandler._get_memory_record",
                return_value=memory,
            ):
                result = await api.update_memory()

        assert result["status"] == "ok"
        assert result["data"]["new_memory_id"] == 7
        assert result["data"]["update_mode"] == "in_place"
        api.plugin.initializer.memory_engine.rewrite_memory_in_place.assert_awaited_once()
        api.plugin.initializer.memory_engine.replace_memory.assert_not_awaited()


class TestStructuredUpdateWorkflow:
    @pytest.mark.asyncio
    async def test_detect_related_memories_respects_session_scope(self, api):
        engine = api.plugin.initializer.memory_engine
        source = {
            "id": 1,
            "text": "current",
            "metadata": {
                "session_id": "s1",
                "persona_id": "p1",
                "key_facts": ["用户住在北京"],
            },
        }
        related = {
            "id": 2,
            "text": "related content",
            "metadata": {
                "session_id": "s1",
                "persona_id": "p1",
                "key_facts": ["用户住在北京"],
            },
        }
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "scope": "session",
                "value": {
                    "summary": "edited",
                    "key_facts": ["用户住在示例市"],
                },
                "field_changes": [
                    {
                        "field": "key_facts",
                        "operation": "replace",
                        "before": "用户住在北京",
                        "after": "用户住在示例市",
                    }
                ],
            }
        )
        with _patch_page_request(req), patch.object(
            api.memory_handler,
            "_get_memory_record",
            AsyncMock(return_value=source),
        ), patch.object(
            api.memory_handler,
            "_list_scope_memories",
            AsyncMock(return_value=[source, related]),
        ):
            result = await api.detect_related_memories()

        assert result["status"] == "ok"
        assert [item["memory_id"] for item in result["data"]["items"]] == [2]
        assert result["data"]["items"][0]["modification_type"] == "exact_replace"
        assert result["data"]["items"][0]["proposed_value"]["key_facts"] == [
            "用户住在示例市"
        ]
        assert result["data"]["plan_id"]

    @pytest.mark.asyncio
    async def test_deleted_field_does_not_produce_related_candidates(self, api):
        source = {
            "id": 1,
            "text": "current",
            "metadata": {
                "session_id": "s1",
                "persona_id": "p1",
                "key_facts": ["用户住在北京"],
            },
        }
        related = {
            "id": 2,
            "text": "related",
            "metadata": {
                "session_id": "s1",
                "persona_id": "p1",
                "key_facts": ["用户住在北京"],
            },
        }
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "scope": "session",
                "value": {"summary": "edited", "key_facts": []},
                "field_changes": [
                    {
                        "field": "key_facts",
                        "operation": "remove",
                        "before": "用户住在北京",
                        "after": None,
                    }
                ],
            }
        )
        with _patch_page_request(req), patch.object(
            api.memory_handler,
            "_get_memory_record",
            AsyncMock(return_value=source),
        ), patch.object(
            api.memory_handler,
            "_list_scope_memories",
            AsyncMock(return_value=[source, related]),
        ):
            result = await api.detect_related_memories()

        assert result["status"] == "ok"
        assert result["data"]["changes"] == []
        assert result["data"]["items"] == []

    def test_near_field_match_requires_manual_selection(self, api):
        candidate = {
            "id": 2,
            "text": "用户目前居住在北京市",
            "metadata": {
                "session_id": "s1",
                "key_facts": ["用户目前居住在北京市"],
            },
        }
        planned = api.memory_handler._build_candidate_plan(
            candidate,
            [
                {
                    "change_id": "change-1",
                    "field": "key_facts",
                    "operation": "replace",
                    "before": "用户目前居住在北京",
                    "after": "用户目前居住在示例市",
                }
            ],
        )

        assert planned is not None
        assert planned["modification_type"] == "near_replace"
        assert planned["default_selected"] is False
        assert planned["proposed_value"]["key_facts"] == ["用户目前居住在示例市"]

    @pytest.mark.asyncio
    async def test_start_job_tracks_current_memory_progress(self, api):
        source = {
            "id": 1,
            "text": "old",
            "metadata": {"session_id": "s1", "persona_id": "p1"},
        }
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "value": {"summary": "edited"},
                "scope": "current",
                "update_mode": "in_place",
            }
        )
        update_result = {
            "status": "ok",
            "data": {"old_memory_id": 1, "new_memory_id": 1},
        }
        with _patch_page_request(req), patch.object(
            api.memory_handler,
            "_get_memory_record",
            AsyncMock(return_value=source),
        ), patch.object(
            api.memory_handler,
            "_replace_structured_memory",
            AsyncMock(return_value=update_result),
        ):
            started = await api.start_structured_update_job()
            task = next(iter(api.memory_handler._update_tasks.values()))
            await task

        job_id = started["data"]["job_id"]
        progress_req = _mock_page_request(args={"job_id": job_id})
        with _patch_page_request(progress_req):
            progress = await api.get_structured_update_progress()

        assert progress["status"] == "ok"
        assert progress["data"]["status"] == "completed"
        assert progress["data"]["completed"] == 1
        assert progress["data"]["succeeded"] == 1
        assert progress["data"]["percent"] == 100

    @pytest.mark.asyncio
    async def test_job_reconciles_selected_related_memory(self, api):
        source = {
            "id": 1,
            "text": "source",
            "metadata": {"session_id": "s1", "persona_id": "p1"},
        }
        related = {
            "id": 2,
            "text": "stale",
            "metadata": {
                "session_id": "s1",
                "persona_id": "p1",
                "topics": ["old"],
                "key_facts": ["stale fact"],
                "importance": 0.4,
            },
        }
        processor = api.plugin.initializer.memory_processor
        plan_id = "plan-1"
        plan_item_id = "item-2"
        api.memory_handler._update_plans[plan_id] = {
            "plan_id": plan_id,
            "source_memory_id": 1,
            "source_fingerprint": api.memory_handler._memory_fingerprint(source),
            "source_value": {"summary": "authoritative"},
            "scope": "session",
            "items": [
                {
                    "plan_item_id": plan_item_id,
                    "memory_id": 2,
                    "memory_fingerprint": api.memory_handler._memory_fingerprint(related),
                    "proposed_value": {
                        "summary": "corrected",
                        "topics": ["new"],
                        "key_facts": ["correct fact"],
                        "participants": [],
                        "sentiment": "neutral",
                        "importance": 0.6,
                    },
                    "modifications": [],
                }
            ],
        }
        req = _mock_page_request(
            get_json={
                "memory_id": 1,
                "value": {"summary": "authoritative"},
                "scope": "session",
                "plan_id": plan_id,
                "selected_plan_item_ids": [plan_item_id],
                "risk_acknowledged": True,
                "update_mode": "in_place",
            }
        )
        replace_result = {
            "status": "ok",
            "data": {"old_memory_id": 1, "new_memory_id": 1},
        }
        with _patch_page_request(req), patch.object(
            api.memory_handler,
            "_get_memory_record",
            AsyncMock(side_effect=[source, related]),
        ), patch.object(
            api.memory_handler,
            "_replace_structured_memory",
            AsyncMock(return_value=replace_result),
        ) as replace_mock:
            started = await api.start_structured_update_job()
            task = next(iter(api.memory_handler._update_tasks.values()))
            await task

        job = api.memory_handler._update_jobs[started["data"]["job_id"]]
        assert job["completed"] == 2
        assert job["succeeded"] == 2
        related_update = replace_mock.await_args_list[1].args[3]
        assert related_update["summary"] == "corrected"
        assert related_update["key_facts"] == ["correct fact"]


class TestBatchDeleteMemories:
    @pytest.mark.asyncio
    async def test_empty_list(self, api):
        req = _mock_page_request(get_json={"memory_ids": []})
        with _patch_page_request(req):
            result = await api.batch_delete_memories()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_type(self, api):
        req = _mock_page_request(get_json={"memory_ids": "not-a-list"})
        with _patch_page_request(req):
            result = await api.batch_delete_memories()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_valid_delete(self, api):
        req = _mock_page_request(get_json={"memory_ids": [1, 2, 3]})
        with _patch_page_request(req):
            result = await api.batch_delete_memories()
        assert result["status"] == "ok"
        assert result["data"]["deleted_count"] == 3
        assert result["data"]["total"] == 3

    @pytest.mark.asyncio
    async def test_mixed_valid_invalid_ids(self, api):
        req = _mock_page_request(get_json={"memory_ids": [1, "abc", 3]})
        with _patch_page_request(req):
            result = await api.batch_delete_memories()
        assert result["status"] == "ok"
        assert result["data"]["failed_count"] == 1
        assert "abc" in result["data"]["failed_ids"]


class TestMemoryTransfer:
    @pytest.mark.asyncio
    async def test_export_selected_json(self, api):
        req = _mock_page_request(get_json={"format": "json", "memory_ids": [1]})
        with _patch_page_request(req):
            result = await api.export_memories()

        assert result["status"] == "ok"
        assert result["data"]["memory_count"] == 1
        payload = json.loads(result["data"]["content"])
        assert payload["format"] == "livingmemory"
        assert payload["memories"][0]["content"] == "portable memory"

    @pytest.mark.asyncio
    async def test_import_preview_counts_duplicate_and_invalid(self, api):
        content = json.dumps(
            [
                {"content": "existing memory", "session_id": "s1", "persona_id": "p1"},
                {"content": "new memory", "session_id": "s1", "persona_id": "p1"},
                {"messages": [{"role": "user"}]},
            ]
        )
        req = _mock_page_request(
            get_json={
                "format": "json",
                "content": content,
                "duplicate_strategy": "skip",
                "dry_run": True,
            }
        )
        with _patch_page_request(req):
            result = await api.import_memories()

        assert result["status"] == "ok"
        assert result["data"]["valid_count"] == 2
        assert result["data"]["invalid_count"] == 1
        assert result["data"]["duplicate_count"] == 1
        assert result["data"]["planned_import_count"] == 1

    @pytest.mark.asyncio
    async def test_import_execute_writes_new_identity_safe_metadata(self, api):
        content = json.dumps(
            {
                "content": "new imported memory",
                "session_id": "s2",
                "metadata": {
                    "memory_uid": "remote-uid",
                    "memory_space_id": "remote-space",
                    "memory_layer": "topic",
                    "importance_revision": 8,
                    "topics": ["import"],
                },
            }
        )
        engine = api.plugin.initializer.memory_engine
        engine.add_memory = AsyncMock(return_value=77)
        req = _mock_page_request(
            get_json={
                "format": "json",
                "content": content,
                "duplicate_strategy": "skip",
                "dry_run": False,
            }
        )
        with _patch_page_request(req):
            result = await api.import_memories()

        assert result["status"] == "ok"
        assert result["data"]["imported_ids"] == [77]
        kwargs = engine.add_memory.await_args.kwargs
        assert "memory_uid" not in kwargs["metadata"]
        assert "memory_space_id" not in kwargs["metadata"]
        assert "memory_layer" not in kwargs["metadata"]
        assert kwargs["metadata"]["importance_revision"] == 1
        assert kwargs["metadata"]["importance_reason"] == "memory_import"
        assert kwargs["source_retention_reason"] == "memory_import"


class TestTestRecall:
    @pytest.mark.asyncio
    async def test_empty_query(self, api):
        req = _mock_page_request(get_json={"query": "", "k": 5})
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "error"
        assert "不能为空" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_k(self, api):
        req = _mock_page_request(get_json={"query": "hello", "k": "abc"})
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "error"
        assert "k" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_mode(self, api):
        req = _mock_page_request(get_json={"query": "hello", "mode": "both"})
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "error"
        assert "mode" in result["message"]

    @pytest.mark.asyncio
    async def test_topic_mode_requires_session(self, api):
        req = _mock_page_request(get_json={"query": "hello", "mode": "topic"})
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "error"
        assert "会话 ID" in result["message"]

    @pytest.mark.asyncio
    async def test_recall_rejects_invalid_temporal_constraint(self, api):
        req = _mock_page_request(
            get_json={
                "query": "七月的安排",
                "mode": "timeline",
                "temporal": {"mode": "range"},
            }
        )
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "error"
        assert "时间检索参数无效" in result["message"]

    @pytest.mark.asyncio
    async def test_topic_mode_bypasses_feature_switch_and_skips_timeline_search(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.topic_memory_enabled = False
        engine.search_memories = AsyncMock(return_value=[])
        engine.topic_memory_store = SimpleNamespace(
            find_memory_spaces_for_session=AsyncMock(return_value=["space-1"])
        )
        topic = SimpleNamespace(
            title="Topic title",
            summary="Topic summary",
            importance=0.8,
            status=SimpleNamespace(value="active"),
        )
        topic_result = SimpleNamespace(
            topic_uid="topic-1",
            topic=topic,
            content="Topic title\nTopic summary",
            final_score=0.9,
            relevance_score=0.8,
            embedding_score=0.7,
            keyword_score=0.2,
            base_relevance_score=0.8,
            rerank_score=None,
        )
        outcome = SimpleNamespace(
            results=[topic_result], diagnostics=lambda: {"selected_count": 1}
        )
        engine.topic_recall_pipeline = SimpleNamespace(
            config={"recall_top_k": 1}, search=AsyncMock(return_value=outcome)
        )
        req = _mock_page_request(
            get_json={
                "query": "hello",
                "mode": "topic",
                "session_id": "bot:FriendMessage:user",
                "k": 5,
            }
        )
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "ok"
        assert result["data"]["mode"] == "topic"
        assert result["data"]["results"][0]["metadata"]["memory_layer"] == "topic"
        engine.search_memories.assert_not_awaited()
        assert engine.topic_recall_pipeline.search.await_args.kwargs["final_k"] == 5

    @pytest.mark.asyncio
    async def test_current_mode_uses_timeline_fallback_when_fragments_exist_but_none_match(
        self, api
    ):
        engine = api.plugin.initializer.memory_engine
        engine.topic_memory_enabled = True
        timeline = SimpleNamespace(
            doc_id=7,
            content="报销核对的 Timeline",
            final_score=0.8,
            rrf_score=0.8,
            bm25_score=0.2,
            vector_score=0.8,
            metadata={"memory_uid": "timeline-7", "importance": 0.7},
            score_breakdown={"document_vector_score": 0.8},
        )
        engine.search_memories = AsyncMock(return_value=[timeline])
        engine.topic_memory_store = SimpleNamespace(
            find_memory_spaces_for_session=AsyncMock(return_value=["space-1"])
        )
        topic = SimpleNamespace(
            title="报销核对",
            summary="报销核对详情",
            importance=0.8,
            status=SimpleNamespace(value="active"),
        )
        topic_result = SimpleNamespace(
            topic_uid="topic-1",
            topic=topic,
            content="报销核对\n报销核对详情",
            final_score=0.9,
            relevance_score=0.8,
            embedding_score=0.7,
            keyword_score=0.2,
            base_relevance_score=0.8,
            rerank_score=None,
        )
        topic_outcome = SimpleNamespace(
            results=[topic_result],
            diagnostics=lambda: {"selected_count": 1},
        )
        fragment_outcome = SimpleNamespace(
            results=[],
            available_count=3,
            diagnostics=lambda: {
                "available_count": 3,
                "selected_count": 0,
            },
        )
        select_supplements = MagicMock(return_value=[timeline])
        engine.topic_recall_pipeline = SimpleNamespace(
            config={"recall_top_k": 1, "timeline_supplement_k": 2},
            search=AsyncMock(return_value=topic_outcome),
            search_fragment_supplements=AsyncMock(
                return_value=fragment_outcome
            ),
            select_timeline_supplements=select_supplements,
        )
        req = _mock_page_request(
            get_json={
                "query": "报销",
                "mode": "current",
                "session_id": "bot:FriendMessage:user",
                "k": 5,
            }
        )

        with _patch_page_request(req):
            result = await api.test_recall()

        assert result["status"] == "ok"
        assert any(
            item["metadata"]["memory_layer"] == "timeline_supplement"
            for item in result["data"]["results"]
        )
        select_supplements.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_recall(self, api):
        req = _mock_page_request(get_json={"query": "test", "k": 5})
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "ok"
        assert result["data"]["query"] == "test"
        assert result["data"]["mode"] == "current"
        assert result["data"]["total"] == 0

    @pytest.mark.asyncio
    async def test_completed_recall_is_saved_to_test_history(self, api):
        trace_store = SimpleNamespace(
            record=AsyncMock(return_value="trace-test-1")
        )
        api.plugin.initializer.recall_trace_store = trace_store
        req = _mock_page_request(
            get_json={"query": "六月报销", "k": 3, "mode": "timeline"}
        )

        with _patch_page_request(req):
            result = await api.test_recall()

        assert result["status"] == "ok"
        assert result["data"]["trace_uid"] == "trace-test-1"
        saved = trace_store.record.await_args.kwargs
        assert saved["trace_type"] == "test"
        assert saved["status"] == "completed"
        assert saved["query_text"] == "六月报销"
        assert saved["request_data"]["mode"] == "timeline"
        assert saved["result_data"]["diagnostics"]["mode"] == "timeline"

    @pytest.mark.asyncio
    async def test_timeline_mode_ignores_disabled_production_top_k(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.search_memories = AsyncMock(return_value=[])
        config = api.plugin.initializer.config_manager
        config.get.side_effect = lambda key, default=None: (
            0 if key == "recall_engine.top_k" else default
        )
        req = _mock_page_request(
            get_json={"query": "test", "k": 4, "mode": "timeline"}
        )
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "ok"
        engine.search_memories.assert_awaited()

    @pytest.mark.asyncio
    async def test_current_mode_respects_disabled_production_top_k(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.search_memories = AsyncMock(return_value=[])
        config = api.plugin.initializer.config_manager
        config.get.side_effect = lambda key, default=None: (
            0 if key == "recall_engine.top_k" else default
        )
        req = _mock_page_request(
            get_json={"query": "test", "k": 4, "mode": "current"}
        )
        with _patch_page_request(req):
            result = await api.test_recall()
        assert result["status"] == "ok"
        engine.search_memories.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recall_includes_score_breakdown_for_dashboard(self, api):
        api.plugin.initializer.memory_engine.search_memories = AsyncMock(
            return_value=[
                SimpleNamespace(
                    doc_id=7,
                    content="memory",
                    final_score=0.42,
                    metadata={"session_id": "s1"},
                    score_breakdown={
                        "document_keyword_score": 0.1,
                        "document_vector_score": 0.2,
                        "graph_keyword_score": 0.3,
                        "graph_vector_score": 0.4,
                    },
                )
            ]
        )
        req = _mock_page_request(get_json={"query": "test", "k": 5})
        with _patch_page_request(req):
            result = await api.test_recall()

        item = result["data"]["results"][0]
        assert item["score_breakdown"]["graph_vector_score"] == 0.4
        assert item["metadata"]["document_keyword_score"] == 0.1


class TestSharedTopicQuerySettings:
    @staticmethod
    def _topic_payload():
        return {
            "definitions": {
                "recall_top_k": {
                    "default": 3,
                    "type": "int",
                    "category": "recall",
                }
            },
            "overrides": {},
            "effective": {"recall_top_k": 3},
        }

    @staticmethod
    def _timeline_payload():
        from astrbot_plugin_livingmemory.core.timeline_settings import (
            TIMELINE_SETTING_DEFINITIONS,
            effective_timeline_settings,
        )

        return {
            "definitions": TIMELINE_SETTING_DEFINITIONS,
            "overrides": {"recall_engine.recent_user_weight": 0.30},
            "effective": effective_timeline_settings(
                {"recall_engine.recent_user_weight": 0.30}
            ),
        }

    @staticmethod
    def _user_profile_payload():
        from astrbot_plugin_livingmemory.core.user_profile_settings import (
            USER_PROFILE_SETTING_DEFINITIONS,
            effective_user_profile_settings,
        )

        return {
            "definitions": USER_PROFILE_SETTING_DEFINITIONS,
            "overrides": {},
            "effective": effective_user_profile_settings({}),
        }

    @pytest.mark.asyncio
    async def test_topic_settings_include_shared_query_controls(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.get_topic_runtime_settings = AsyncMock(
            return_value=self._topic_payload()
        )
        engine.topic_build_manager = SimpleNamespace(
            has_active_builds=lambda: False
        )
        api.plugin.initializer.get_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )

        result = await api.get_topic_settings()

        assert result["status"] == "ok"
        data = result["data"]
        assert data["effective"]["recall_engine.recent_user_weight"] == 0.30
        assert data["definitions"]["recall_engine.inject_with_recent_context"][
            "shared_query_setting"
        ] is True

    @pytest.mark.asyncio
    async def test_topic_update_splits_topic_and_shared_changes(self, api):
        engine = api.plugin.initializer.memory_engine
        engine.get_topic_runtime_settings = AsyncMock(
            return_value=self._topic_payload()
        )
        engine.update_topic_runtime_settings = AsyncMock(
            return_value=self._topic_payload()
        )
        engine.topic_build_manager = SimpleNamespace(
            has_active_builds=lambda: False
        )
        initializer = api.plugin.initializer
        initializer.get_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )
        initializer.update_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )
        req = _mock_page_request(
            get_json={
                "changes": {
                    "recall_top_k": 4,
                    "recall_engine.recent_user_weight": 0.25,
                },
                "reset_keys": ["recall_engine.assistant_context_mode"],
                "reset_all": False,
            }
        )

        with _patch_page_request(req):
            result = await api.update_topic_settings()

        assert result["status"] == "ok"
        engine.update_topic_runtime_settings.assert_awaited_once_with(
            {"recall_top_k": 4}, reset_keys=[], reset_all=False
        )
        initializer.update_timeline_runtime_settings.assert_awaited_once_with(
            {"recall_engine.recent_user_weight": 0.25},
            reset_keys=["recall_engine.assistant_context_mode"],
            reset_all=False,
        )

    @pytest.mark.asyncio
    async def test_unified_settings_are_classified_by_backend(self, api):
        from astrbot_plugin_livingmemory.core.topic_settings import (
            TOPIC_SETTING_DEFINITIONS,
            effective_topic_settings,
        )

        engine = api.plugin.initializer.memory_engine
        engine.get_topic_runtime_settings = AsyncMock(
            return_value={
                "definitions": TOPIC_SETTING_DEFINITIONS,
                "overrides": {},
                "effective": effective_topic_settings({}),
            }
        )
        engine.topic_build_manager = SimpleNamespace(has_active_builds=lambda: False)
        engine.get_user_profile_runtime_settings = AsyncMock(
            return_value=self._user_profile_payload()
        )
        api.plugin.initializer.get_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )
        req = _mock_page_request(args={})

        with _patch_page_request(req):
            result = await api.get_settings()

        assert result["status"] == "ok"
        data = result["data"]
        assert data["schema_revision"] == 1
        assert data["definitions"]["recall_top_k"]["settings_category"] == "recall"
        assert data["definitions"]["related_topic_top_n"]["settings_group"] == "topic_relations"
        assert data["definitions"]["recall_engine.recent_user_weight"]["views"] == ["timeline", "topic"]
        assert {item["id"] for item in data["categories"]} >= {
            "recall", "timeline", "topic", "user_profile"
        }

    @pytest.mark.asyncio
    async def test_unified_settings_update_splits_internal_owners(self, api):
        from astrbot_plugin_livingmemory.core.topic_settings import (
            TOPIC_SETTING_DEFINITIONS,
            effective_topic_settings,
        )

        engine = api.plugin.initializer.memory_engine
        topic_payload = {
            "definitions": TOPIC_SETTING_DEFINITIONS,
            "overrides": {},
            "effective": effective_topic_settings({}),
        }
        engine.get_topic_runtime_settings = AsyncMock(return_value=topic_payload)
        engine.update_topic_runtime_settings = AsyncMock(return_value=topic_payload)
        engine.topic_build_manager = SimpleNamespace(has_active_builds=lambda: False)
        engine.get_user_profile_runtime_settings = AsyncMock(
            return_value=self._user_profile_payload()
        )
        engine.update_user_profile_runtime_settings = AsyncMock(
            return_value=self._user_profile_payload()
        )
        initializer = api.plugin.initializer
        initializer.get_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )
        initializer.update_timeline_runtime_settings = AsyncMock(
            return_value=self._timeline_payload()
        )
        req = _mock_page_request(
            get_json={
                "changes": {
                    "recall_top_k": 4,
                    "recall_engine.recent_user_weight": 0.25,
                },
                "reset_keys": ["recall_engine.assistant_context_mode"],
            }
        )

        with _patch_page_request(req):
            result = await api.update_settings()

        assert result["status"] == "ok"
        engine.update_topic_runtime_settings.assert_awaited_once_with(
            {"recall_top_k": 4}, reset_keys=[], reset_all=False
        )
        initializer.update_timeline_runtime_settings.assert_awaited_once_with(
            {"recall_engine.recent_user_weight": 0.25},
            reset_keys=["recall_engine.assistant_context_mode"],
            reset_all=False,
        )


class TestGraphEndpoints:
    @pytest.mark.asyncio
    async def test_overview_invalid_params(self, api):
        req = _mock_page_request(
            args={
                "session_id": "",
                "persona_id": "",
                "limit_memories": "abc",
                "limit_entries": "36",
                "limit_nodes": "48",
                "limit_edges": "72",
            }
        )
        with _patch_page_request(req):
            result = await api.get_graph_overview()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_overview_no_graph_store(self, api):
        req = _mock_page_request(
            args={
                "session_id": "",
                "persona_id": "",
                "limit_memories": "12",
                "limit_entries": "36",
                "limit_nodes": "48",
                "limit_edges": "72",
            }
        )
        with _patch_page_request(req):
            result = await api.get_graph_overview()
        assert result["status"] == "ok"
        assert result["data"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_query_invalid_params(self, api):
        req = _mock_page_request(
            get_json={
                "query": "test",
                "limit_memories": "abc",
            }
        )
        with _patch_page_request(req):
            result = await api.query_graph()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_query_empty_no_graph_store(self, api):
        req = _mock_page_request(
            get_json={
                "query": "",
                "session_id": "",
                "limit_memories": 10,
                "limit_entries": 40,
                "limit_nodes": 56,
                "limit_edges": 96,
            }
        )
        with _patch_page_request(req):
            result = await api.query_graph()
        assert result["status"] == "ok"
        assert result["data"]["enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_filter", [None, "None"])
    async def test_query_expands_node_hits_without_text_recall(self, session_filter):
        snapshot = {
            "nodes": [
                {
                    "id": 56,
                    "type": "person",
                    "weight": 1.0,
                    "degree": 0,
                    "label": "luna",
                }
            ],
            "edges": [],
            "entries": [
                {
                    "id": 501,
                    "memory_id": 123,
                    "entry_type": "summary",
                    "relation_type": "mentions",
                    "content": "Luna appears in this memory",
                    "metadata": {"session_id": "s1"},
                    "node_ids": [56],
                }
            ],
            "memories": [
                {
                    "memory_id": 123,
                    "summary": "Luna appears in this memory",
                    "importance": 0.7,
                    "entry_count": 1,
                    "node_count": 1,
                    "edge_count": 0,
                }
            ],
        }
        graph_store = SimpleNamespace(
            search_nodes_by_tokens=AsyncMock(
                return_value=[
                    {
                        "id": 56,
                        "node_key": "person:luna",
                        "node_type": "person",
                        "node_value": "luna",
                        "canonical_value": "luna",
                        "metadata": {},
                    }
                ]
            ),
            get_entries_for_node_ids=AsyncMock(
                return_value=[
                    {
                        "entry_id": 501,
                        "source_memory_id": 123,
                        "content": "Luna appears in this memory",
                        "metadata": {"session_id": "s1"},
                        "score": 0.85,
                    }
                ]
            ),
            get_subgraph_for_memories=AsyncMock(return_value=snapshot),
        )
        engine = FakeMemoryEngine(graph_store=graph_store)
        engine.search_memories = AsyncMock(return_value=[])
        api = PluginPageApi(FakePlugin(memory_engine=engine))
        req = _mock_page_request(
            get_json={
                "query": "luna",
                "session_id": session_filter,
                "persona_id": "undefined",
            }
        )

        with _patch_page_request(req):
            result = await api.query_graph()

        assert result["status"] == "ok"
        data = result["data"]
        assert data["filters"]["session_id"] is None
        assert data["filters"]["persona_id"] is None
        assert data["matched_node_ids"] == [56]
        assert data["matched_memory_ids"] == [123]
        assert data["summary"]["visible_node_count"] == 1
        assert data["snapshot"]["nodes"][0]["highlighted"] is True
        assert data["retrieval"]["items"][0]["source"] == "graph_node"
        engine.search_memories.assert_awaited_once()
        assert engine.search_memories.call_args.kwargs["session_id"] is None
        graph_store.get_entries_for_node_ids.assert_awaited_once()
        assert (
            graph_store.get_entries_for_node_ids.call_args.kwargs["session_id"] is None
        )
        graph_store.get_subgraph_for_memories.assert_awaited_once()
        assert graph_store.get_subgraph_for_memories.call_args.args[0] == [123]


class TestListBackups:
    @pytest.mark.asyncio
    async def test_no_data_dir(self, api):
        api.plugin.initializer.data_dir = ""
        result = await api.list_backups()
        assert result["data"]["backups"] == []
        assert result["data"]["total"] == 0

    @pytest.mark.asyncio
    async def test_no_initializer(self, api):
        api.plugin.initializer = None
        result = await api.list_backups()
        assert result["data"]["backups"] == []
        assert result["data"]["total"] == 0

    @pytest.mark.asyncio
    async def test_with_backup_dir(self, api):
        with patch(
            "astrbot_plugin_livingmemory.core.managers.backup_manager.BackupManager.list_backups",
            return_value=[],
        ):
            result = await api.list_backups()
        assert result["data"]["backups"] == []
        assert result["data"]["total"] == 0


# ---------------------------------------------------------------------------
# Ensure plugin ready helper
# ---------------------------------------------------------------------------


class TestEnsurePluginReady:
    @pytest.mark.asyncio
    async def test_ready_returns_components(self, api):
        components, error = await api._ensure_plugin_ready()
        assert error is None
        assert components is not None
        assert "memory_engine" in components
        assert isinstance(components["memory_engine"], FakeMemoryEngine)

    @pytest.mark.asyncio
    async def test_not_ready_returns_error(self, api_not_ready):
        components, error = await api_not_ready._ensure_plugin_ready()
        assert components is None
        assert error is not None
        assert error["status"] == "error"

    @pytest.mark.asyncio
    async def test_no_memory_engine(self, api):
        api.plugin.initializer.memory_engine = None
        components, error = await api._ensure_plugin_ready()
        assert components is None
        assert error["status"] == "error"
        assert "未初始化" in error["message"]


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_registers_all_routes(self):
        plugin = FakePlugin()
        api = PluginPageApi(plugin)
        api.register_routes()
        assert len(plugin._api_routes) == 104

        paths = {route for route, _, _, _ in plugin._api_routes}
        prefix = PAGE_API_PREFIX
        assert f"{prefix}/stats" in paths
        assert f"{prefix}/sessions" in paths
        assert f"{prefix}/sessions/audit" in paths
        assert f"{prefix}/sessions/maintenance/preview" in paths
        assert f"{prefix}/sessions/maintenance/start" in paths
        assert f"{prefix}/sessions/maintenance/task" in paths
        assert f"{prefix}/sessions/maintenance/tasks" in paths
        assert f"{prefix}/sessions/maintenance/tasks/delete" in paths
        assert f"{prefix}/sessions/maintenance/tasks/clear" in paths
        assert f"{prefix}/memories" in paths
        assert f"{prefix}/memories/update" in paths
        assert f"{prefix}/memories/update/stage" in paths
        assert f"{prefix}/memories/related" in paths
        assert f"{prefix}/user-profiles" in paths
        assert f"{prefix}/user-profiles/build-candidates" in paths
        assert f"{prefix}/user-profiles/build" in paths
        assert f"{prefix}/user-profiles/detail" in paths
        assert f"{prefix}/user-profiles/facts/action" in paths
        assert f"{prefix}/user-profiles/relationship/update" in paths
        assert f"{prefix}/user-profiles/accounts/bind/preview" in paths
        assert f"{prefix}/user-profiles/share-groups/save" in paths
        assert f"{prefix}/memories/update/start" in paths
        assert f"{prefix}/timeline/staged-edits" in paths
        assert f"{prefix}/timeline/staged-edits/apply" in paths
        assert f"{prefix}/timeline/staged-edits/delete" in paths
        assert f"{prefix}/timeline/inactive" in paths
        assert f"{prefix}/timeline/inactive/restore" in paths
        assert f"{prefix}/memories/update/progress" in paths
        assert f"{prefix}/memories/batch-delete" in paths
        assert f"{prefix}/memories/export" in paths
        assert f"{prefix}/memories/import" in paths
        assert f"{prefix}/timeline/settings" in paths
        assert f"{prefix}/timeline/settings/update" in paths
        assert f"{prefix}/timeline/rebuild/preview" in paths
        assert f"{prefix}/timeline/rebuild/start" in paths
        assert f"{prefix}/timeline/rebuild/task" in paths
        assert f"{prefix}/timeline/rebuild/tasks" in paths
        assert f"{prefix}/timeline/rebuild/resume" in paths
        assert f"{prefix}/timeline/rebuild/cancel" in paths
        assert f"{prefix}/timeline/rebuild/tasks/delete" in paths
        assert f"{prefix}/timeline/rebuild/tasks/clear" in paths
        assert f"{prefix}/settings" in paths
        assert f"{prefix}/settings/update" in paths
        assert f"{prefix}/recall/test" in paths
        assert f"{prefix}/recall/traces" in paths
        assert f"{prefix}/recall/traces/detail" in paths
        assert f"{prefix}/recall/traces/settings" in paths
        assert f"{prefix}/recall/traces/delete" in paths
        assert f"{prefix}/recall/traces/clear" in paths
        assert f"{prefix}/graph/overview" in paths
        assert f"{prefix}/graph/query" in paths
        assert f"{prefix}/backups" in paths
        assert f"{prefix}/database/health" in paths
        assert f"{prefix}/database/repair" in paths
        assert f"{prefix}/database/repair/progress" in paths
        assert f"{prefix}/topics/overview" in paths
        assert f"{prefix}/topics" in paths
        assert f"{prefix}/topics/detail" in paths
        assert f"{prefix}/topics/settings" in paths
        assert f"{prefix}/topics/settings/update" in paths
        assert f"{prefix}/topics/maintenance/unindexed" in paths
        assert f"{prefix}/topics/maintenance/preview" in paths
        assert f"{prefix}/topics/maintenance/clear" in paths
        assert f"{prefix}/topics/archived/delete" in paths
        assert f"{prefix}/topics/maintenance/revectorize" in paths
        assert f"{prefix}/topics/relations/recompute" in paths
        assert f"{prefix}/topics/build/start" in paths
        assert f"{prefix}/topics/build/progress" in paths
        assert f"{prefix}/topics/build/discard" in paths
        assert f"{prefix}/topics/reviews" in paths
        assert f"{prefix}/topics/reviews/detail" in paths
        assert f"{prefix}/topics/reviews/resolve" in paths
        assert f"{prefix}/topics/governance/preview" in paths
        assert f"{prefix}/topics/governance/execute" in paths
        assert f"{prefix}/models" in paths
        assert f"{prefix}/models/test" in paths
        assert f"{prefix}/identities" in paths
        assert f"{prefix}/identities/save" in paths
        assert f"{prefix}/identities/topics/sync" not in paths
        assert f"{prefix}/identities/impact" not in paths

    def test_route_prefix_contains_plugin_name(self):
        assert PLUGIN_NAME in PAGE_API_PREFIX


@pytest.mark.asyncio
async def test_page_api_shutdown_drains_topic_handler(api):
    api.topic_handler.shutdown = AsyncMock()

    await api.shutdown()

    api.topic_handler.shutdown.assert_awaited_once()
