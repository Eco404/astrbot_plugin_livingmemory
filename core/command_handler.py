"""
命令处理器
负责处理插件命令
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .base.config_manager import ConfigManager
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .validators.index_validator import IndexValidator


class CommandHandler:
    """Command handler"""

    def __init__(
        self,
        context,
        config_manager: ConfigManager,
        memory_engine: MemoryEngine | None,
        conversation_manager: ConversationManager | None,
        index_validator: IndexValidator | None,
        memory_processor=None,
        webui_server=None,
        initialization_status_callback=None,
    ):
        """
        初始化命令处理器

        Args:
            context: AstrBot Context
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            index_validator: 索引验证器
            memory_processor: 记忆处理器（用于手动总结）
            webui_server: WebUI服务器
            initialization_status_callback: 初始化状态回调函数
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.index_validator = index_validator
        self._memory_processor = memory_processor
        self.webui_server = webui_server
        self.get_initialization_status = initialization_status_callback

    @staticmethod
    def _format_error_message(
        action: str, error: Exception, suggestions: list[str] | None = None
    ) -> str:
        """Format user-facing error message with actionable hints."""
        message = [f"{action} failed.", f"Error details: {error}"]
        if suggestions:
            message.append("")
            message.append("Troubleshooting suggestions:")
            for index, suggestion in enumerate(suggestions, start=1):
                message.append(f"{index}. {suggestion}")
        return "\n".join(message)

    @staticmethod
    def _component_not_ready_message(component: str, command: str) -> str:
        """Build a consistent component-not-ready response."""
        return (
            f"{command} failed: {component} is not initialized.\n"
            "Please run /lmem status to check plugin status; if abnormal, check startup logs."
        )

    async def handle_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem status command"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("Memory Engine", "/lmem status")
            )
            return

        try:
            stats = await self.memory_engine.get_statistics()

            # Format time
            last_update = "Never"
            if stats.get("newest_memory"):
                last_update = datetime.fromtimestamp(stats["newest_memory"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            # Calculate database size
            db_size = 0.0
            if os.path.exists(self.memory_engine.db_path):
                db_size = os.path.getsize(self.memory_engine.db_path) / (1024 * 1024)

            session_count = len(stats.get("sessions", {}))

            message = f"""LivingMemory Status Report

Total Memories: {stats["total_memories"]}
Sessions: {session_count}
Last Updated: {last_update}
Database Size: {db_size:.2f} MB

Available Operations:
- /lmem search <keyword>
- /lmem webui"""

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Failed to get status: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Get status",
                    e,
                    [
                        "Confirm database file is readable/writable",
                        "Confirm memory engine has completed initialization",
                        "Check logs for exception stack trace to locate specific module",
                    ],
                )
            )

    async def handle_search(
        self, event: AstrMessageEvent, query: str, k: int = 5
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem search command"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("Memory Engine", "/lmem search")
            )
            return

        # Input validation
        if not query or not query.strip():
            yield event.plain_result(
                "Query keyword cannot be empty. Example: /lmem search project progress 5"
            )
            return

        # Limit k to range 1-100
        k = max(1, min(k, 100))

        try:
            session_id = event.unified_msg_origin
            results = await self.memory_engine.search_memories(
                query=query.strip(), k=k, session_id=session_id
            )

            if not results:
                yield event.plain_result(
                    f"No memories found related to '{query}'. Try shorter keywords, "
                    "or increase the return count parameter k."
                )
                return

            message = f"Found {len(results)} related memories:\n\n"
            for i, result in enumerate(results, 1):
                score = result.final_score
                content = (
                    result.content[:100] + "..."
                    if len(result.content) > 100
                    else result.content
                )
                raw_breakdown = getattr(result, "score_breakdown", {})
                breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
                message += f"{i}. [Score:{score:.2f}] {content}\n"
                message += f"   ID: {result.doc_id}\n\n"
                message += (
                    "   Hit: "
                    f"Doc Keyword={breakdown.get('document_keyword_score', 0.0):.2f}, "
                    f"Doc Vector={breakdown.get('document_vector_score', 0.0):.2f}, "
                    f"Graph Keyword={breakdown.get('graph_keyword_score', 0.0):.2f}, "
                    f"Graph Vector={breakdown.get('graph_vector_score', 0.0):.2f}\n\n"
                )

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Search failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Search",
                    e,
                    [
                        "Confirm keyword is not empty and has reasonable length",
                        "Confirm database and index files exist and are readable/writable",
                        "Check logs for retrieval component initialization failure",
                    ],
                )
            )

    async def handle_forget(
        self, event: AstrMessageEvent, doc_id: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem forget command"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("Memory Engine", "/lmem forget")
            )
            return

        # Input validation
        if doc_id < 0:
            yield event.plain_result("Memory ID must be a non-negative integer. Example: /lmem forget 123")
            return

        try:
            success = await self.memory_engine.delete_memory(doc_id)
            if success:
                yield event.plain_result(f"Deleted memory #{doc_id}.")
            else:
                yield event.plain_result(
                    f"Delete failed: Memory #{doc_id} does not exist.\n"
                    "Please use /lmem search or WebUI to confirm the memory ID first."
                )
        except Exception as e:
            logger.error(f"Delete failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Delete memory",
                    e,
                    [
                        "Confirm memory ID exists and belongs to currently accessible data",
                        "Confirm database is not locked by other processes for extended periods",
                        "Check logs for delete call stack trace",
                    ],
                )
            )

    async def handle_rebuild_index(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem rebuild-index command"""
        if not self.memory_engine or not self.index_validator:
            yield event.plain_result(
                self._component_not_ready_message(
                    "Memory Engine or Index Validator", "/lmem rebuild-index"
                )
            )
            return

        try:
            yield event.plain_result("Starting index status check...")

            # Check index consistency
            status = await self.index_validator.check_consistency()

            if status.is_consistent and not status.needs_rebuild:
                yield event.plain_result(f"Index status normal: {status.reason}")
                return

            # Show current status
            status_msg = f"""Current Index Status:
• Documents Table: {status.documents_count} entries
• BM25 Index: {status.bm25_count} entries
• Vector Index: {status.vector_count} entries
• Issue: {status.reason}

Starting index rebuild..."""
            yield event.plain_result(status_msg)

            # Execute rebuild
            result = await self.index_validator.rebuild_indexes(self.memory_engine)

            if result["success"]:
                partial_notice = ""
                if result.get("partial"):
                    partial_notice = (
                        "\nNote: A small number of failures occurred in this rebuild, failure rate "
                        f"{result.get('failure_ratio', 0):.2%}, within safe threshold."
                        "You can run /lmem rebuild-index again later to complete."
                    )
                result_msg = f"""Index Rebuild Complete.

Results:
• Success: {result["processed"]} entries
• Failed: {result["errors"]} entries
• Total: {result["total"]} entries
• Vector Processing Mode: {result.get("vector_mode", "unknown")}
• New Index Switched: {"Yes" if result.get("switched") else "No"}
{partial_notice}

You can now continue using the recall function."""
                yield event.plain_result(result_msg)
            else:
                yield event.plain_result(
                    "Index rebuild failed.\n"
                    f"Error details: {result.get('message', 'Unknown error')}\n"
                    "Please check logs to confirm failure reason, then retry /lmem rebuild-index."
                )

        except Exception as e:
            logger.error(f"Index rebuild failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Rebuild index",
                    e,
                    [
                        "Confirm Embedding Provider is available",
                        "Confirm database and index files are readable/writable",
                        "Locate failed documents from logs and retry rebuild",
                    ],
                )
            )

    async def handle_rebuild_graph(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem rebuild-graph command"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("Memory Engine", "/lmem rebuild-graph")
            )
            return

        try:
            yield event.plain_result("Starting graph memory index rebuild, this may take some time...")
            result = await self.memory_engine.rebuild_graph_index()
            yield event.plain_result(
                "Graph memory rebuild complete.\n\n"
                f"• Rebuilt: {result.get('rebuilt', 0)} entries\n"
                f"• Skipped: {result.get('skipped', 0)} entries"
            )
        except Exception as e:
            logger.error(f"Graph memory rebuild failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Rebuild graph memory",
                    e,
                    [
                        "Confirm graph memory feature is enabled",
                        "Confirm database and index files are readable/writable",
                        "Check logs to locate specific failed documents",
                    ],
                )
            )

    async def handle_webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem webui command"""
        webui_url = self._get_webui_url()

        if not webui_url:
            message = """WebUI feature is currently disabled.

Possible reasons:
1. webui.enabled=false in configuration
2. WebUI service startup failed (check logs)

Currently available features:
• /lmem status - View system status
• /lmem search - Search memories
• /lmem forget - Delete memories"""
        else:
            message = f"""LivingMemory WebUI

Access URL: {webui_url}

WebUI Features:
• Memory editing and management
• Visual statistical analysis
• Advanced configuration management
• System debugging tools
• Data migration management

More complex management operations can be performed in WebUI."""

        yield event.plain_result(message)

    async def handle_summarize(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem summarize command - trigger memory summarization immediately"""
        if not self.conversation_manager or not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message(
                    "Conversation Manager or Memory Engine", "/lmem summarize"
                )
            )
            return

        session_id = event.unified_msg_origin
        try:
            # Get current message count and summarization progress
            actual_count = await self.conversation_manager.store.get_message_count(
                session_id
            )
            last_summarized_index = (
                await self.conversation_manager.get_session_metadata(
                    session_id, "last_summarized_index", 0
                )
            )
            try:
                last_summarized_index = int(last_summarized_index)
            except (TypeError, ValueError):
                last_summarized_index = 0

            unsummarized = actual_count - last_summarized_index

            if unsummarized < 2:
                yield event.plain_result(
                    "No new conversations need summarization.\n"
                    f"Total messages: {actual_count}\n"
                    f"Summarized up to message index: {last_summarized_index}"
                )
                return

            yield event.plain_result(
                f"Starting manual memory summarization...\n"
                f"Message range: [{last_summarized_index}:{actual_count}], {unsummarized} messages total"
            )

            history_messages = await self.conversation_manager.get_messages_range(
                session_id=session_id,
                start_index=last_summarized_index,
                end_index=actual_count,
            )

            if not history_messages:
                yield event.plain_result(
                    "Failed to get messages: no messages available for summarization.\n"
                    "Please confirm current session has historical messages and retry."
                )
                return

            # Get persona_id
            from .utils import get_persona_id

            persona_id = await get_persona_id(self.context, event)

            # Determine if group chat
            is_group_chat = bool(
                history_messages[0].group_id if history_messages else False
            )
            if not is_group_chat and "GroupMessage" in session_id:
                is_group_chat = True

            if not self._memory_processor:
                yield event.plain_result(
                    self._component_not_ready_message("Memory Processor", "/lmem summarize")
                )
                return

            (
                content,
                metadata,
                importance,
            ) = await self._memory_processor.process_conversation(
                messages=history_messages,
                is_group_chat=is_group_chat,
                persona_id=persona_id,
            )

            metadata["source_window"] = {
                "session_id": session_id,
                "start_index": last_summarized_index,
                "end_index": actual_count,
                "message_count": actual_count - last_summarized_index,
                "triggered_by": "manual",
            }

            await self.memory_engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
            )

            await self.conversation_manager.update_session_metadata(
                session_id, "last_summarized_index", actual_count
            )
            await self.conversation_manager.update_session_metadata(
                session_id, "pending_summary", None
            )

            topics = ", ".join(metadata.get("topics", [])) or "None"
            yield event.plain_result(
                f"Memory summarization complete.\n"
                f"Importance: {importance:.2f}\n"
                f"Topics: {topics}\n"
                f"Updated summarization progress to message {actual_count}"
            )

        except Exception as e:
            logger.error(f"Manual memory summarization trigger failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Memory summarization",
                    e,
                    [
                        "Confirm current session has at least 2 unsummarized messages",
                        "Confirm LLM Provider responds normally",
                        "Check logs for summary processing stack trace",
                    ],
                )
            )

    async def handle_reset(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem reset command"""
        if not self.conversation_manager:
            yield event.plain_result(
                self._component_not_ready_message("Conversation Manager", "/lmem reset")
            )
            return

        session_id = event.unified_msg_origin
        try:
            await self.conversation_manager.clear_session(session_id)
            message = (
                "Long-term memory context for current session has been reset.\n\n"
                "Next memory summarization will start from now and will not include previous conversation content."
            )
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Manual memory context reset failed: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Reset memory context",
                    e,
                    [
                        "Confirm session ID is valid and session storage is accessible",
                        "Confirm database is not locked",
                        "Check logs for clear_session call stack trace",
                    ],
                )
            )

    async def handle_cleanup(
        self, event: AstrMessageEvent, dry_run: bool = False
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem cleanup command - clean up memory injection fragments from AstrBot historical messages"""
        session_id = event.unified_msg_origin
        try:
            mode_text = "Preview mode: " if dry_run else ""
            yield event.plain_result(
                f"{mode_text}Starting cleanup of memory injection fragments from AstrBot historical messages..."
            )

            # Check if context is available
            if not self.context:
                yield event.plain_result(
                    "Cleanup failed: Cannot access AstrBot Context.\n"
                    "Please confirm plugin is running in a complete AstrBot context and retry."
                )
                return

            # Get current conversation ID
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            if not cid:
                yield event.plain_result("Current session has no conversation history, no cleanup needed.")
                return

            # 获取对话历史
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, cid
            )
            if not conversation or not conversation.history:
                yield event.plain_result("Current conversation history is empty, no cleanup needed.")
                return

            # Clean up memory injection fragments from historical messages
            import json
            import re

            from .base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

            # 解析 history（字符串格式）
            try:
                history = json.loads(conversation.history)
            except json.JSONDecodeError:
                yield event.plain_result(
                    "解析对话历史失败：数据不是有效 JSON。\n"
                    "请检查会话存储内容是否被外部工具修改。"
                )
                return

            # Statistics
            stats = {
                "scanned": len(history),
                "matched": 0,
                "cleaned": 0,
                "deleted": 0,
            }

            # Compile cleanup regex
            pattern = re.compile(
                re.escape(MEMORY_INJECTION_HEADER)
                + r".*?"
                + re.escape(MEMORY_INJECTION_FOOTER),
                flags=re.DOTALL,
            )

            # Clean up historical messages
            cleaned_history = []
            for msg in history:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    cleaned_history.append(msg)
                    continue

                # Check if contains injection markers
                if (
                    MEMORY_INJECTION_HEADER in content
                    and MEMORY_INJECTION_FOOTER in content
                ):
                    stats["matched"] += 1

                    # Clean content
                    cleaned_content = pattern.sub("", content)
                    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()

                    # If empty after cleanup, skip this message
                    if not cleaned_content:
                        stats["deleted"] += 1
                        logger.debug(
                            f"[cleanup] Deleted pure memory injection message: role={msg.get('role')}"
                        )
                        continue

                    # If content remains after cleanup, keep cleaned message
                    if cleaned_content != content:
                        msg_copy = msg.copy()
                        msg_copy["content"] = cleaned_content
                        cleaned_history.append(msg_copy)
                        stats["cleaned"] += 1
                        logger.debug(
                            f"[cleanup] Cleaned memory fragments inside message: "
                            f"original_length={len(content)}, new_length={len(cleaned_content)}"
                        )
                        continue

                cleaned_history.append(msg)

            # If not preview mode, update database
            if not dry_run and (stats["cleaned"] > 0 or stats["deleted"] > 0):
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=session_id,
                    conversation_id=cid,
                    history=cleaned_history,
                )
                logger.info(
                    f"[{session_id}] cleanup updated AstrBot conversation history: "
                    f"cleaned={stats['cleaned']}, deleted={stats['deleted']}"
                )

            # Format results
            message = f"""{mode_text}Cleanup complete.

Statistics:
• Scanned Messages: {stats["scanned"]} entries
• Matched Memory Fragments: {stats["matched"]} entries
• Cleaned Content: {stats["cleaned"]} entries
• Deleted Messages: {stats["deleted"]} entries

{"This is preview mode, no actual data was modified. Use /lmem cleanup exec to perform actual cleanup." if dry_run else "AstrBot conversation history has been updated, memory injection fragments have been cleaned up."}"""

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"Failed to clean up historical messages: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    "Clean up historical messages",
                    e,
                    [
                        "Confirm current session has readable historical records",
                        "Confirm conversation storage is readable/writable",
                        "Check logs for cleanup call stack trace",
                    ],
                )
            )

    async def handle_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """Handle /lmem help command"""
        message = """LivingMemory User Guide

Core Commands:
/lmem status              View system status
/lmem search <keyword> [count]  Search memories (default 5)
/lmem forget <ID>          Delete specified memory
/lmem rebuild-index       Rebuild index (fix index inconsistency)
/lmem rebuild-graph       Rebuild graph memory index (backfill old memories)
/lmem webui               Open WebUI management panel
/lmem summarize           Immediately trigger memory summarization for current session
/lmem reset               Reset current session memory context
/lmem cleanup [preview|exec] Clean up memory fragments from historical messages (default preview)
/lmem help                Show this help

Usage Tips:
• Use search command for daily queries
• Use WebUI for complex management
• Memories are automatically saved from conversations
• Use forget to delete sensitive information
• Execute rebuild-index when index is inconsistent
• Execute rebuild-graph to backfill old data after enabling graph memory
• Execute cleanup to clean up old data after plugin update

cleanup command examples:
  /lmem cleanup          # Preview mode, shows statistics only
  /lmem cleanup preview  # Same as above
  /lmem cleanup exec     # Perform actual cleanup

More info: https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory"""

        yield event.plain_result(message)

    def _get_webui_url(self) -> str | None:
        """Get WebUI access URL"""
        webui_config = self.config_manager.webui_settings
        if not webui_config.get("enabled") or not self.webui_server:
            return None

        host = webui_config.get("host", "127.0.0.1")
        port = webui_config.get("port", 8080)

        if host in ["0.0.0.0", ""]:
            return f"http://127.0.0.1:{port}"
        else:
            return f"http://{host}:{port}"
