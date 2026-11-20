"""Мост amemo mcp к работающей службе (amemo/mcp_bridge.py): выбор моста, пересылка сообщений, новая сессия после
перезапуска службы, служба не отвечает. Служба — настоящий uvicorn в подпроцессе на схеме теста."""

import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from amemo import mcp_bridge, settings
from amemo.service import mcp
from test_mcp import INIT, sync
from test_service_contract import _free_port

pytest_plugins = ["test_service_write", "test_cli"]     # фикстуры env, base

ROOT = str(Path(mcp_bridge.__file__).resolve().parents[1])     # код этого дерева, а не установленный
AGENT = "model:bridge"
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _clean() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("AMEMO_", "CLAUDECODE", "CODEX_"))
            and k != "PYTHONPATH"}


@pytest.fixture
def service(env, base):
    """Служба на свободном порту над базой фикстуры base; env — окружение, в котором её видит amemo mcp."""
    port = _free_port()
    environ = {**_clean(), **env, "AMEMO_PORT": str(port), "PYTHONPATH": ROOT}
    child = subprocess.Popen([sys.executable, "-m", "amemo.service", "--port", str(port)], env=environ,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                    if r.read() == b"ok":
                        break
            except OSError:
                pass
            assert child.poll() is None, child.stdout.read().decode()
            assert time.monotonic() < deadline, "служба не ответила"
            time.sleep(0.2)
        with urllib.request.urlopen(f"{url}/api/v1/base", timeout=5) as r:
            base_id = json.loads(r.read())["base_id"]
        yield SimpleNamespace(url=url, env=environ, base_id=base_id, child=child)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=30)


def _line(message) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


def _answers(out: io.BytesIO) -> dict:
    return {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}


def _request(mid, method, **params):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


def test_headers_are_those_of_the_service():
    assert (mcp_bridge.AGENT_HEADER, mcp_bridge.SESSION_HEADER) == (mcp.AGENT_HEADER, mcp.SESSION_HEADER)


# --- выбор моста ---

def test_find_takes_the_running_service_of_the_same_base(service):
    s = settings.load(service.env)
    assert mcp_bridge.find(service.env, s, lambda: service.base_id) == service.url
    assert mcp_bridge.find(service.env, s, lambda: "another-base") is None
    assert mcp_bridge.find({**service.env, "AMEMO_MCP_BRIDGE": "0"}, s, lambda: service.base_id) is None


def test_find_without_a_service_does_not_touch_the_base(env):
    environ = {**env, "AMEMO_PORT": str(_free_port())}
    assert mcp_bridge.find(environ, settings.load(environ), lambda: pytest.fail("base_id is not needed")) is None


# --- пересылка ---

def test_bridge_relays_initialize_tools_and_calls(service):
    out = io.BytesIO()
    lines = [INIT, INITIALIZED, _request(2, "tools/list"),
             _request(3, "tools/call", name="amemo_search", arguments={"query": "анкер М10"})]
    bridge = mcp_bridge.Bridge(service.url, AGENT, out)
    mcp_bridge.serve(bridge, io.BytesIO(b"".join(map(_line, lines))))
    answers = _answers(out)
    assert set(answers) == {1, 2, 3}
    assert answers[1]["result"]["protocolVersion"] and bridge.protocol == answers[1]["result"]["protocolVersion"]
    assert {t["name"] for t in answers[2]["result"]["tools"]} == set(mcp.TOOLS)
    result = answers[3]["result"]
    assert result["isError"] is False and result["structuredContent"]["items"]


def test_bridge_opens_a_new_session_when_the_service_forgot_the_old_one(service):
    """Служба перезапустилась (обновление) — её сессия забыта: мост повторяет initialize и тот же вызов."""
    out = io.BytesIO()
    bridge = mcp_bridge.Bridge(service.url, AGENT, out)
    bridge.send(_line(INIT), INIT)
    bridge.send(_line(INITIALIZED), INITIALIZED)
    first = bridge.session
    bridge.session = "0" * 32                    # сессии с таким id у службы нет — 404
    bridge.send(_line(_request(2, "tools/list")), _request(2, "tools/list"))
    answers = _answers(out)
    assert {t["name"] for t in answers[2]["result"]["tools"]} == set(mcp.TOOLS)
    assert bridge.session not in (None, first, "0" * 32)


def test_bridge_answers_an_error_when_the_service_does_not_answer():
    out = io.BytesIO()
    bridge = mcp_bridge.Bridge(f"http://127.0.0.1:{_free_port()}", AGENT, out, delays=(0, 0))
    bridge.send(_line(_request(7, "tools/list")), _request(7, "tools/list"))
    bridge.send(_line(INITIALIZED), INITIALIZED)        # уведомлению ответа нет
    answers = _answers(out)
    assert set(answers) == {7}
    assert answers[7]["error"]["code"] == mcp_bridge.SERVER_ERROR
    assert "not answering" in answers[7]["error"]["message"]


# --- amemo mcp ---

def test_amemo_mcp_goes_through_the_running_service(service):
    """amemo mcp при работающей службе своей базы — мост: служба остановилась — вызов отвечает «not answering»
    (работай amemo mcp сам, он ответил бы из базы)."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "amemo.cli", "mcp", "--agent", AGENT],
                                   env=service.env)

    async def main():
        with anyio.fail_after(90):
            async with Client(params, mode="legacy") as client:
                tools = (await client.list_tools()).tools
                assert {t.name for t in tools} == set(mcp.TOOLS)
                found = await client.call_tool("amemo_search", {"query": "анкер М10"})
                assert not found.is_error and found.structured_content["items"]
                service.child.terminate()
                service.child.wait(timeout=30)
                with pytest.raises(Exception, match="not answering"):
                    await client.call_tool("amemo_search", {"query": "анкер М10"})
    sync(main)
