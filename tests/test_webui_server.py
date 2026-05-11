"""Tests for WebUIServer lifecycle behavior."""

import asyncio
from types import SimpleNamespace

import astrbot_plugin_livingmemory.webui.server as webui_server_mod
import pytest
from astrbot_plugin_livingmemory.webui.server import WebUIServer


@pytest.mark.asyncio
async def test_webui_start_failure_does_not_raise_system_exit(monkeypatch):
    class FailingServer:
        started = False
        should_exit = False

        def __init__(self, config):
            self.config = config

        async def serve(self):
            raise SystemExit(1)

    monkeypatch.setattr(webui_server_mod.uvicorn, "Server", FailingServer)

    server = WebUIServer(
        memory_engine=SimpleNamespace(config={}),
        config={
            "host": "127.0.0.1",
            "port": 18081,
            "access_password": "test-password",
        },
    )

    with pytest.raises(RuntimeError, match="WebUI 启动失败"):
        await server.start()

    assert server._server is None
    assert server._server_task is None
    assert server._cleanup_task is None


@pytest.mark.asyncio
async def test_webui_stop_cancels_server_task_after_timeout(monkeypatch):
    class HangingServer:
        started = True
        should_exit = False

        def __init__(self, config):
            self.config = config

        async def serve(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(webui_server_mod.uvicorn, "Server", HangingServer)

    server = WebUIServer(
        memory_engine=SimpleNamespace(config={}),
        config={
            "host": "127.0.0.1",
            "port": 18082,
            "access_password": "test-password",
            "shutdown_timeout": 0.01,
        },
    )
    await server.start()

    await server.stop()

    assert server._server is None
    assert server._server_task is None
    assert server._cleanup_task is None
    assert server._server_error is None
