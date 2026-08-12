"""
Tests for PluginInitializer state management and provider resolution.
"""

import hashlib
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import astrbot_plugin_livingmemory.core.plugin_initializer as plugin_initializer_mod
import pytest
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.base.exceptions import InitializationError
from astrbot_plugin_livingmemory.core.plugin_initializer import PluginInitializer
from astrbot_plugin_livingmemory.storage.topic_memory_store import TopicMemoryStore


@pytest.fixture
def mock_context():
    context = Mock()
    context.get_provider_by_id = Mock(return_value=None)
    context.get_all_embedding_providers = Mock(return_value=[])
    context.get_using_provider = Mock(return_value=None)
    return context


@pytest.fixture
def initializer(mock_context, tmp_path):
    return PluginInitializer(mock_context, ConfigManager(), str(tmp_path))


def test_initializer_default_state(initializer):
    assert initializer.is_initialized is False
    assert initializer.is_failed is False
    assert initializer.error_message is None


@pytest.mark.asyncio
async def test_user_profile_persona_resolver_reads_current_prompt_and_digest(
    mock_context, tmp_path
):
    prompt = "你重视真诚，也保留自己的判断。"
    mock_context.persona_manager = Mock()
    mock_context.persona_manager.get_persona = AsyncMock(
        return_value=SimpleNamespace(
            name="Companion", system_prompt=f"  {prompt}\n"
        )
    )
    init = PluginInitializer(mock_context, ConfigManager(), str(tmp_path))

    persona = await init.resolve_user_profile_persona("persona-1")

    assert persona["persona_id"] == "persona-1"
    assert persona["name"] == "Companion"
    assert persona["prompt"] == prompt
    assert persona["signature"] == {
        "algorithm": "sha256",
        "digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_timeline_v3_import_preserves_newly_moved_legacy_settings(
    mock_context, tmp_path
):
    db_path = tmp_path / "livingmemory.db"
    store = TopicMemoryStore(str(db_path))
    await store.initialize()
    await store.update_timeline_setting_overrides(
        {"__legacy_imported_v1__": True, "__legacy_imported_v2__": True},
        settings_revision=2,
    )
    config = ConfigManager(
        {"graph_memory": {"expansion_limit": 42}}
    )
    init = PluginInitializer(mock_context, config, str(tmp_path))

    await init._initialize_timeline_runtime_settings(str(db_path))

    assert config.get("graph_memory.expansion_limit") == 42
    stored = await store.get_timeline_setting_overrides()
    assert stored["graph_memory.expansion_limit"] == 42
    assert stored["__legacy_imported_v3__"] is True


@pytest.mark.asyncio
async def test_timeline_settings_migrate_continuation_cap_above_legacy_base(
    mock_context, tmp_path
):
    db_path = tmp_path / "livingmemory.db"
    store = TopicMemoryStore(str(db_path))
    await store.initialize()
    await store.update_timeline_setting_overrides(
        {
            "__legacy_imported_v4__": True,
            "reflection_engine.summary_trigger_rounds": 50,
        },
        settings_revision=7,
    )
    config = ConfigManager({})
    init = PluginInitializer(mock_context, config, str(tmp_path))

    await init._initialize_timeline_runtime_settings(str(db_path))

    assert config.get("reflection_engine.summary_trigger_rounds") == 50
    assert (
        config.get("reflection_engine.topic_continuation_force_summary_rounds")
        == 100
    )
    stored = await store.get_timeline_setting_overrides()
    assert stored["reflection_engine.topic_continuation_force_summary_rounds"] == 100


@pytest.mark.asyncio
async def test_ensure_initialized_timeout(initializer):
    ok = await initializer.ensure_initialized(timeout=0.1)
    assert ok is False


def test_initialize_providers_with_fallback(monkeypatch, mock_context, tmp_path):
    class DummyEmbeddingProvider:
        pass

    class DummyProvider:
        pass

    # make isinstance checks pass
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.EmbeddingProvider",
        DummyEmbeddingProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.Provider",
        DummyProvider,
    )

    emb = DummyEmbeddingProvider()
    llm = DummyProvider()
    mock_context.get_provider_by_id.return_value = None
    mock_context.get_all_embedding_providers.return_value = [emb]
    mock_context.get_using_provider.return_value = llm

    init = PluginInitializer(mock_context, ConfigManager(), str(tmp_path))
    init._initialize_providers(silent=True)

    assert init.embedding_provider is emb
    assert init.llm_provider is llm


def test_cloudflare_rerank_overrides_astrbot_reranker(
    monkeypatch, mock_context, tmp_path
):
    class DummyEmbeddingProvider:
        pass

    class DummyProvider:
        pass

    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.EmbeddingProvider",
        DummyEmbeddingProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.Provider",
        DummyProvider,
    )
    emb = DummyEmbeddingProvider()
    llm = DummyProvider()
    mock_context.get_all_embedding_providers.return_value = [emb]
    mock_context.get_using_provider.return_value = llm
    config = ConfigManager(
        {
            "cloudflare_rerank": {
                "enabled": True,
                "account_id": "account",
                "api_token": "token",
            }
        }
    )

    init = PluginInitializer(mock_context, config, str(tmp_path))
    init._initialize_providers(silent=True)

    assert init.rerank_provider is not None
    assert init.rerank_provider.provider_config["id"] == (
        "cloudflare_workers_ai_rerank"
    )
    assert init.rerank_provider.model == "@cf/baai/bge-reranker-base"
    assert init.rerank_initialization_error is None


def test_cloudflare_rerank_initializes_while_llm_provider_is_still_unavailable(
    mock_context, tmp_path
):
    mock_context.get_all_providers.return_value = []
    mock_context.get_all_embedding_providers.return_value = []
    config = ConfigManager(
        {
            "provider_settings": {"rerank_provider_id": ""},
            "cloudflare_rerank": {
                "enabled": True,
                "account_id": "account",
                "api_token": "token",
            },
        }
    )
    init = PluginInitializer(mock_context, config, str(tmp_path))

    init._initialize_providers(silent=True)

    assert init.llm_provider is None
    assert init.rerank_provider is not None
    assert init.rerank_provider.provider_config["id"] == (
        "cloudflare_workers_ai_rerank"
    )


def test_builtin_cloudflare_rerank_has_priority_over_astrbot_rerank_id(
    monkeypatch, mock_context, tmp_path
):
    class DummyAstrBotReranker:
        async def rerank(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.RerankProvider",
        DummyAstrBotReranker,
    )
    astrbot_reranker = DummyAstrBotReranker()
    mock_context.get_provider_by_id.return_value = astrbot_reranker
    config = ConfigManager(
        {
            "provider_settings": {"rerank_provider_id": "astrbot-reranker"},
            "cloudflare_rerank": {
                "enabled": True,
                "account_id": "account",
                "api_token": "token",
            },
        }
    )
    init = PluginInitializer(mock_context, config, str(tmp_path))

    init._initialize_rerank_provider(silent=False)

    assert init.rerank_provider is not astrbot_reranker
    assert init.rerank_provider.provider_config["id"] == (
        "cloudflare_workers_ai_rerank"
    )


def test_check_faiss_runtime_raises_actionable_error(monkeypatch, initializer):
    result = subprocess.CompletedProcess(
        args=[],
        returncode=-4,
        stdout="",
        stderr="Illegal instruction",
    )
    monkeypatch.setattr(
        plugin_initializer_mod.subprocess, "run", Mock(return_value=result)
    )

    with pytest.raises(InitializationError, match="FAISS 初始化失败"):
        initializer._check_faiss_runtime()


def test_check_faiss_runtime_falls_back_to_generic(monkeypatch, initializer):
    failed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="optimized import failed"
    )
    succeeded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    run = Mock(side_effect=[failed, succeeded])
    monkeypatch.setattr(plugin_initializer_mod.subprocess, "run", run)
    monkeypatch.delenv("FAISS_OPT_LEVEL", raising=False)

    initializer._check_faiss_runtime()

    assert plugin_initializer_mod.os.environ["FAISS_OPT_LEVEL"] == "generic"
    assert run.call_count == 2
    assert run.call_args_list[1].kwargs["env"]["FAISS_OPT_LEVEL"] == "generic"


def test_check_faiss_runtime_reports_python_binding_mismatch(
    monkeypatch, initializer
):
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="NameError: name 'SuperKMeans' is not defined",
    )
    run = Mock(return_value=result)
    monkeypatch.setattr(plugin_initializer_mod.subprocess, "run", run)
    monkeypatch.setattr(
        plugin_initializer_mod.metadata,
        "version",
        Mock(return_value="1.14.2"),
    )

    with pytest.raises(InitializationError) as exc_info:
        initializer._check_faiss_runtime()

    message = str(exc_info.value)
    assert "Python 封装与本地二进制扩展不匹配" in message
    assert "1.14.2" in message
    assert "重新安装兼容版本" in message
    assert run.call_count == 1


def test_load_faiss_vec_db_class_uses_patched_class(monkeypatch, initializer):
    class FakeFaissVecDB:
        pass

    monkeypatch.setattr(plugin_initializer_mod, "FaissVecDB", FakeFaissVecDB)

    assert initializer._load_faiss_vec_db_class() is FakeFaissVecDB


@pytest.mark.asyncio
async def test_wait_for_providers_non_blocking_success(initializer):
    initializer._initialize_providers = Mock()
    initializer.embedding_provider = object()
    initializer.llm_provider = object()

    ok = await initializer._wait_for_providers_non_blocking(max_wait=0.1)
    assert ok is True


@pytest.mark.asyncio
async def test_retry_task_done_callback_clears_state(initializer):
    task = Mock()
    task.done.return_value = True
    task.cancelled.return_value = False
    task.exception.return_value = None
    initializer._retry_task = task

    initializer._on_retry_task_done(task)
    assert initializer._retry_task is None


@pytest.mark.asyncio
async def test_retry_initialization_timeout_sets_actionable_error(initializer):
    initializer._max_provider_attempts = 0
    initializer._provider_check_attempts = 0

    await initializer._retry_initialization()

    assert initializer.is_failed is True
    assert initializer.error_message is not None
    assert "Provider 初始化超时" in initializer.error_message
    assert "请检查 provider_settings 配置" in initializer.error_message


@pytest.mark.asyncio
async def test_complete_initialization_wires_graph_db_and_engine_config(
    monkeypatch, mock_context, tmp_path
):
    created_vec_dbs = []

    class DummyEmbeddingProvider:
        pass

    class DummyProvider:
        pass

    class FakeFaissVecDB:
        def __init__(self, db_path, index_path, embedding_provider):
            self.db_path = db_path
            self.index_path = index_path
            self.embedding_provider = embedding_provider
            created_vec_dbs.append(self)

        async def initialize(self):
            return None

    class FakeDBMigration:
        def __init__(self, db_path):
            self.db_path = db_path

    class FakeMemoryEngine:
        def __init__(
            self, db_path, faiss_db, graph_vector_db, llm_provider=None,
            rerank_provider=None, config=None, identity_profile_store=None,
            topic_provider_resolver=None,
        ):
            self.db_path = db_path
            self.faiss_db = faiss_db
            self.graph_vector_db = graph_vector_db
            self.llm_provider = llm_provider
            self.rerank_provider = rerank_provider
            self.identity_profile_store = identity_profile_store
            self.topic_provider_resolver = topic_provider_resolver
            self.config = config or {}
            self.text_processor = Mock(async_init=AsyncMock())

        async def initialize(self):
            return None

        def set_session_scope_resolver(self, resolver):
            self.session_scope_resolver = resolver

    class FakeConversationStore:
        def __init__(self, db_path):
            self.db_path = db_path

        async def initialize(self):
            return None

        async def sync_message_counts(self):
            return []

    class FakeConversationManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.store = kwargs["store"]

        async def get_session_scope(self, session_id):
            return None

    class FakeMemoryProcessor:
        def __init__(self, context=None, llm_provider=None, **kwargs):
            self.context = context
            self.llm_provider = llm_provider
            self.config = kwargs.get("config", {})
            self.identity_profile_store = kwargs.get("identity_profile_store")

    class FakeIndexValidator:
        def __init__(self, db_path, db):
            self.db_path = db_path
            self.db = db

    class FakeDecayScheduler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            return None

    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.EmbeddingProvider",
        DummyEmbeddingProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.Provider",
        DummyProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.FaissVecDB",
        FakeFaissVecDB,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.DBMigration",
        FakeDBMigration,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.MemoryEngine",
        FakeMemoryEngine,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.ConversationStore",
        FakeConversationStore,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.ConversationManager",
        FakeConversationManager,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.MemoryProcessor",
        FakeMemoryProcessor,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.IndexValidator",
        FakeIndexValidator,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.DecayScheduler",
        FakeDecayScheduler,
    )

    init = PluginInitializer(
        mock_context,
        ConfigManager(
            {
                "migration_settings": {"auto_migrate": False},
                "importance_decay": {"decay_rate": 0},
                "forgetting_agent": {"auto_cleanup_enabled": False},
                "graph_memory": {
                    "enabled": True,
                    "document_route_weight": 0.7,
                    "graph_route_weight": 0.3,
                    "cross_route_bonus": 0.12,
                    "expansion_limit": 12,
                    "max_topics_per_memory": 4,
                    "max_participants_per_memory": 5,
                    "max_facts_per_memory": 6,
                    "atom_enabled": False,
                    "atom_maintenance_interval_hours": 12.0,
                    "atom_forget_delay_days": 3.0,
                },
            }
        ),
        str(tmp_path),
    )
    init.embedding_provider = DummyEmbeddingProvider()
    init.llm_provider = DummyProvider()
    init._check_and_fix_dimension_mismatch = AsyncMock()
    init._repair_message_counts = AsyncMock()
    init._auto_rebuild_index_if_needed = AsyncMock()

    await init._complete_initialization()

    assert len(created_vec_dbs) == 2
    assert created_vec_dbs[1].db_path.endswith("livingmemory_graph_documents.db")
    assert created_vec_dbs[1].index_path.endswith("livingmemory_graph.index")
    assert init.memory_engine.graph_vector_db is init.graph_db
    assert init.memory_engine.config["graph_memory_enabled"] is True
    assert init.memory_engine.config["document_route_weight"] == 0.7
    assert init.memory_engine.config["graph_route_weight"] == 0.3
    assert init.memory_engine.config["cross_route_bonus"] == 0.12
    assert init.memory_engine.config["graph_expansion_limit"] == 12
    assert init.memory_engine.config["graph_max_topics"] == 4
    assert init.memory_engine.config["graph_max_participants"] == 5
    assert init.memory_engine.config["graph_max_facts"] == 6
    assert init.memory_engine.config["atom_enabled"] is False
    assert init.memory_engine.config["atom_maintenance_interval_hours"] == 12.0
    assert init.memory_engine.config["atom_forget_delay_days"] == 3.0
    assert init.memory_processor.config.get("atom_enabled") is False
    assert init.identity_profile_store is init.memory_engine.identity_profile_store
    assert init.identity_profile_store is init.memory_processor.identity_profile_store
    assert init.identity_profile_store.path == (
        tmp_path / "authoritative_identities.json"
    )


@pytest.mark.asyncio
async def test_complete_initialization_skips_graph_db_when_disabled(
    monkeypatch, mock_context, tmp_path
):
    created_vec_dbs = []

    class DummyEmbeddingProvider:
        pass

    class DummyProvider:
        pass

    class FakeFaissVecDB:
        def __init__(self, db_path, index_path, embedding_provider):
            self.db_path = db_path
            self.index_path = index_path
            self.embedding_provider = embedding_provider
            created_vec_dbs.append(self)

        async def initialize(self):
            return None

    class FakeDBMigration:
        def __init__(self, db_path):
            self.db_path = db_path

    class FakeMemoryEngine:
        def __init__(
            self, db_path, faiss_db, graph_vector_db, llm_provider=None,
            rerank_provider=None, config=None, identity_profile_store=None,
            topic_provider_resolver=None,
        ):
            self.db_path = db_path
            self.faiss_db = faiss_db
            self.graph_vector_db = graph_vector_db
            self.llm_provider = llm_provider
            self.rerank_provider = rerank_provider
            self.topic_provider_resolver = topic_provider_resolver
            self.config = config or {}
            self.text_processor = Mock(async_init=AsyncMock())

        async def initialize(self):
            return None

        def set_session_scope_resolver(self, resolver):
            self.session_scope_resolver = resolver

    class FakeConversationStore:
        def __init__(self, db_path):
            self.db_path = db_path

        async def initialize(self):
            return None

        async def sync_message_counts(self):
            return []

    class FakeConversationManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.store = kwargs["store"]

        async def get_session_scope(self, session_id):
            return None

    class FakeMemoryProcessor:
        def __init__(self, context=None, llm_provider=None, **kwargs):
            self.context = context
            self.llm_provider = llm_provider

    class FakeIndexValidator:
        def __init__(self, db_path, db):
            self.db_path = db_path
            self.db = db

    class FakeDecayScheduler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            return None

    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.EmbeddingProvider",
        DummyEmbeddingProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.Provider",
        DummyProvider,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.FaissVecDB",
        FakeFaissVecDB,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.DBMigration",
        FakeDBMigration,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.MemoryEngine",
        FakeMemoryEngine,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.ConversationStore",
        FakeConversationStore,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.ConversationManager",
        FakeConversationManager,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.MemoryProcessor",
        FakeMemoryProcessor,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.IndexValidator",
        FakeIndexValidator,
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.plugin_initializer.DecayScheduler",
        FakeDecayScheduler,
    )

    init = PluginInitializer(
        mock_context,
        ConfigManager(
            {
                "migration_settings": {"auto_migrate": False},
                "importance_decay": {"decay_rate": 0},
                "forgetting_agent": {"auto_cleanup_enabled": False},
                "graph_memory": {"enabled": False},
            }
        ),
        str(tmp_path),
    )
    init.embedding_provider = DummyEmbeddingProvider()
    init.llm_provider = DummyProvider()
    init._check_and_fix_dimension_mismatch = AsyncMock()
    init._repair_message_counts = AsyncMock()
    init._auto_rebuild_index_if_needed = AsyncMock()

    await init._complete_initialization()

    assert len(created_vec_dbs) == 1
    assert init.graph_db is None
    assert init.memory_engine.graph_vector_db is None
    assert init.memory_engine.config["graph_memory_enabled"] is False
    init._check_and_fix_dimension_mismatch.assert_awaited_once()
