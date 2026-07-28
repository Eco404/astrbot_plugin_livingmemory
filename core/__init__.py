"""LivingMemory public core API with cycle-safe lazy imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ConfigManager": (".base", "ConfigManager"),
    "ConfigurationError": (".base", "ConfigurationError"),
    "DatabaseError": (".base", "DatabaseError"),
    "InitializationError": (".base", "InitializationError"),
    "LivingMemoryException": (".base", "LivingMemoryException"),
    "MemoryProcessingError": (".base", "MemoryProcessingError"),
    "ProviderNotReadyError": (".base", "ProviderNotReadyError"),
    "RetrievalError": (".base", "RetrievalError"),
    "ValidationError": (".base", "ValidationError"),
    "ConversationManager": (".managers.conversation_manager", "ConversationManager"),
    "GraphMemoryManager": (".managers.graph_memory_manager", "GraphMemoryManager"),
    "MemoryEngine": (".managers.memory_engine", "MemoryEngine"),
    "ExtractedGraph": (".models", "ExtractedGraph"),
    "GraphEdge": (".models", "GraphEdge"),
    "GraphEntry": (".models", "GraphEntry"),
    "GraphNode": (".models", "GraphNode"),
    "MemoryEvent": (".models", "MemoryEvent"),
    "Message": (".models", "Message"),
    "Session": (".models", "Session"),
    "ChatroomContextParser": (".processors.chatroom_parser", "ChatroomContextParser"),
    "EntityResolver": (".processors.entity_resolver", "EntityResolver"),
    "GraphExtractor": (".processors.graph_extractor", "GraphExtractor"),
    "MemoryProcessor": (".processors.memory_processor", "MemoryProcessor"),
    "TextProcessor": (".processors.text_processor", "TextProcessor"),
    "store_round_with_length_check": (
        ".processors.message_utils",
        "store_round_with_length_check",
    ),
    "IndexValidator": (".validators.index_validator", "IndexValidator"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
