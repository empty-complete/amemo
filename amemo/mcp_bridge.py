"""amemo mcp — мост к работающей службе: строки JSON-RPC со stdin уходят POST-ом на её /mcp, ответы — в stdout.

Служба держит пул, модель смыслового поиска и код последнего обновления — окна агентов не поднимают каждое свои.
Мост выбирается при запуске (find): служба отвечает на /health и /api/v1/base без токена, и база у неё та же,
что у этого процесса; иначе amemo mcp работает сам (service.mcp.open_stdio). AMEMO_MCP_BRIDGE=0 — всегда сам.

Служба перезапустилась (обновление) — её сессия MCP забыта (404): мост повторяет сохранённый initialize клиента
и тот же вызов. Порт не принимает соединение — несколько повторов (RETRY_DELAYS), затем ошибка JSON-RPC на этот
вызов, а следующий снова идёт в службу. Обрыв после отправки не повторяется: вызов мог записать — агент
сначала проверяет (amemo_search/amemo_get).

Модуль лёгкий (только стандартная библиотека): мосту не нужны SDK MCP и драйвер базы, а всё, что он вызывает,
импортируется при запуске — обновление файлов amemo под живым мостом его не ломает."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO

log = logging.getLogger(__name__)

AGENT_HEADER = "x-amemo-agent"           # как service.mcp.AGENT_HEADER (тест сверяет)
SESSION_HEADER = "mcp-session-id"        # как service.mcp.SESSION_HEADER
PROTOCOL_HEADER = "mcp-protocol-version"
WILDCARD = ("0.0.0.0", "::", "")         # адрес «на всех интерфейсах» — к службе идём по 127.0.0.1
PROBE_TIMEOUT = 2                        # с на /health и /api/v1/base при выборе моста
CONNECT_TIMEOUT = 3                      # с на соединение со службой: порт молчит — служба не отвечает
REQUEST_TIMEOUT = 600                    # с на ответ службы: долгий инструмент (пачка, импорт) успевает
RETRY_DELAYS = (0.5, 1, 2, 4)            # с между повторами, пока порт службы не принимает соединение
WORKERS = 8                              # вызовов клиента параллельно (агент шлёт несколько разом)
SERVER_ERROR = -32000                    # код JSON-RPC ошибки моста
INITIALIZED = b'{"jsonrpc":"2.0","method":"notifications/initialized"}'


def service_url(settings) -> str:
    """Адрес службы этой машины из настроек: адрес привязки (вместо 0.0.0.0 — 127.0.0.1) и порт."""
    host = "127.0.0.1" if settings.host in WILDCARD else settings.host
    return f"http://{f'[{host}]' if ':' in host else host}:{settings.port}"


class _NotSent(OSError):
    """Соединение со службой не открылось — запрос не ушёл, его можно повторить."""


def _http(url: str, method: str, body: bytes | None = None, headers: Mapping[str, str] | None = None, *,
          timeout: float, connect: float = CONNECT_TIMEOUT):
    """Один запрос: (статус, заголовки, тело). Соединение — за connect с (иначе _NotSent), ответ — за timeout с.
    Прокси окружения и системы не участвуют: служба — на этой машине или по AMEMO_URL."""
    parts = urllib.parse.urlsplit(url)
    kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = kind(parts.hostname, parts.port, timeout=connect)
    try:
        try:
            conn.connect()
        except OSError as e:
            raise _NotSent(e) from e
        conn.sock.settimeout(timeout)
        conn.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), body=body,
                     headers=dict(headers or {}))
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def find(env: Mapping[str, str], settings, base_id: Callable[[], str | None], *,
         timeout: float = PROBE_TIMEOUT) -> str | None:
    """Адрес службы для моста или None (amemo mcp работает сам). Служба — AMEMO_URL, иначе адрес из настроек;
    она отвечает «ok» на /health, отдаёт /api/v1/base без токена, и её base_id совпадает с base_id() — базой
    этого процесса (base_id() зовётся, только если служба ответила)."""
    if env.get("AMEMO_MCP_BRIDGE", "").strip() == "0":
        return None
    url = env.get("AMEMO_URL", "").strip().rstrip("/") or service_url(settings)
    try:
        status, _, body = _http(f"{url}/health", "GET", timeout=timeout, connect=timeout)
        if status != 200 or body.decode("utf-8", "replace").strip() != "ok":
            return None
        status, _, body = _http(f"{url}/api/v1/base", "GET", timeout=timeout, connect=timeout)
        theirs = json.loads(body)["base_id"] if status == 200 else None
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return None                       # не запущена, просит токен, не amemo — работаем сами
    return url if theirs and theirs == base_id() else None


class Bridge:
    """Пересылка сообщений одного клиента в /mcp службы; ответы пишутся в out по строке под замком."""

    def __init__(self, url: str, agent: str, out: BinaryIO, *, timeout: float = REQUEST_TIMEOUT,
                 delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.endpoint = url.rstrip("/") + "/mcp"
        self.agent, self.out, self.timeout, self.delays = agent, out, timeout, delays
        self.session: str | None = None
        self.protocol: str | None = None
        self.initialize: bytes | None = None     # initialize клиента — для новой сессии после перезапуска службы
        self._write = threading.Lock()
        self._renew = threading.Lock()

    # --- сообщения ---

    def send(self, line: bytes, message=None) -> None:
        """Одно сообщение клиента в службу; ответ (если он есть) — строкой в out. Не бросает."""
        request = isinstance(message, dict) and "method" in message and "id" in message
        mid = message["id"] if request else None
        try:
            if request and message["method"] == "initialize":
                self.initialize, self.session, self.protocol = line, None, None
                status, headers, body = self._post(line, None)
                if status == 200:
                    self._take(headers, body)
            else:
                status, headers, body = self._exchange(line)
        except (OSError, http.client.HTTPException, _Refused) as e:
            if request:
                self._error(mid, self._unreachable(e))
            return
        except Exception as e:                  # noqa: BLE001 — мост не падает на одном сообщении
            log.exception("bridge: the message was not relayed")
            if request:
                self._error(mid, f"the amemo bridge failed on this call: {type(e).__name__}: {e}")
            return
        self._reply(mid if request else None, status, headers, body)

    def _exchange(self, line: bytes):
        session = self.session
        status, headers, body = self._post(line, session)
        if status == 404 and session is not None and self.initialize is not None:
            self._renew_session(session)        # служба перезапустилась и забыла сессию
            status, headers, body = self._post(line, self.session)
        return status, headers, body

    def _renew_session(self, stale: str) -> None:
        with self._renew:
            if self.session != stale:
                return                          # новую сессию уже открыл другой поток
            status, headers, body = self._post(self.initialize, None)
            if status != 200:
                raise _Refused(f"the service refused a new MCP session: HTTP {status}")
            self._take(headers, body)
            self._post(INITIALIZED, self.session)

    def _take(self, headers, body: bytes) -> None:
        """Сессия и версия протокола из ответа на initialize."""
        self.session = headers.get(SESSION_HEADER)
        for message in _messages(headers, body):
            version = (message.get("result") or {}).get("protocolVersion") if isinstance(message, dict) else None
            if version:
                self.protocol = version

    # --- HTTP ---

    def _post(self, body: bytes, session: str | None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   AGENT_HEADER: self.agent}
        if session:
            headers[SESSION_HEADER] = session
        if self.protocol:
            headers[PROTOCOL_HEADER] = self.protocol
        for delay in (*self.delays, None):
            try:
                return _http(self.endpoint, "POST", body, headers, timeout=self.timeout)
            except _NotSent:
                if delay is None:         # повторяется только неоткрытое соединение: служба перезапускается
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self) -> None:
        """Конец ввода: закрыть сессию в службе (если не выйдет — она истечёт сама)."""
        if self.session is None:
            return
        try:
            _http(self.endpoint, "DELETE", headers={AGENT_HEADER: self.agent, SESSION_HEADER: self.session},
                  timeout=PROBE_TIMEOUT, connect=PROBE_TIMEOUT)
        except (OSError, http.client.HTTPException):
            pass

    # --- ответы ---

    def _reply(self, mid, status: int, headers, body: bytes) -> None:
        try:
            messages = _messages(headers, body)
        except ValueError:
            messages = []
        if status == 200 and messages:
            for message in messages:
                self._line(message)
            return
        if status in (200, 202) and mid is None:
            return                              # уведомление или ответ клиента: службе нечего сказать
        if mid is None:
            log.warning("bridge: the service answered HTTP %s to a notification", status)
            return
        self._error(mid, f"the amemo service answered HTTP {status}: {_detail(messages) or 'no details'}")

    def _unreachable(self, e: BaseException) -> str:
        if isinstance(e, _Refused):
            return str(e)
        if isinstance(e, _NotSent):
            return (f"the amemo service at {self.endpoint} is not answering ({e.__cause__ or e}) — it may be "
                    f"restarting after an update; repeat the call in a minute")
        return (f"the connection to the amemo service broke during the call ({type(e).__name__}: {e}); a write may "
                f"have happened — check with amemo_search or amemo_get before repeating it")

    def _error(self, mid, text: str) -> None:
        self._line({"jsonrpc": "2.0", "id": mid, "error": {"code": SERVER_ERROR, "message": text}})

    def _line(self, message) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write:
            self.out.write(data)
            self.out.flush()


class _Refused(Exception):
    """Служба ответила, но новую сессию не открыла."""


def _messages(headers, body: bytes) -> list:
    """Сообщения JSON-RPC тела: JSON (один объект или массив) или поток SSE (строки data:)."""
    if not body.strip():
        return []
    kind = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        found = [json.loads(line[5:]) for line in body.decode("utf-8").splitlines() if line.startswith("data:")
                 and line[5:].strip()]
    else:
        found = json.loads(body)
    return found if isinstance(found, list) else [found]


def _detail(messages: list) -> str | None:
    """Текст ошибки из тела: ошибка JSON-RPC (message) или problem+json службы (code: detail)."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        error = message.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if message.get("detail"):
            return f"{message.get('code', 'error')}: {message['detail']}"
    return None


def serve(bridge: Bridge, stdin: BinaryIO, *, workers: int = WORKERS) -> None:
    """Читать stdin до конца: initialize, уведомления и ответы клиента — по порядку, вызовы — параллельно."""
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="amemo-mcp-bridge") as pool:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            call = isinstance(message, dict) and "id" in message and message.get("method") not in (None, "initialize")
            if call:
                pool.submit(bridge.send, line, message)
            else:
                bridge.send(line, message)      # порядок важен: сессия и initialized — раньше вызовов
    bridge.close()
