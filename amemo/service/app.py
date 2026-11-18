"""Приложение Starlette: create_app(settings) — пул соединений, защита и сессия, problem+json, маршруты.

Библиотека синхронная, поэтому обработчики — обычные функции: Starlette зовёт их в пуле потоков, соединение берётся из
psycopg_pool на запрос (autocommit — как db.connect: каждый conn.transaction() — настоящая транзакция). lifespan
открывает пул и готовит базу (db.prepare — миграции, словари, base_id), при остановке закрывает пул. Секрет cookie
человека — новый на каждый запуск службы. Подборки и загрузки старше суток (AMEMO_STATE) и брошенные временные файлы
загрузки (.amemo-upload-*.part в корне AMEMO_DOCS) lifespan удаляет при запуске и затем раз в час фоновой задачей. /mcp — Streamable HTTP сервера MCP (amemo/service/mcp.py): его менеджер сессий
lifespan запускает и останавливает вместе с пулом.

Смысловой поиск включён всегда (embed.keep): фоновая задача через KEEP_START после запуска и затем раз в KEEP_EVERY
досчитывает вектора новых и изменённых записей, а если активной модели нет, а файлы модели на месте, — включает её.
Человеку для этого ничего делать не нужно; AMEMO_BACKEND=none выключает канал, как и раньше.

Обновления (amemo/updates.py): Updater службы — app.state.updates. lifespan завершает переход после перезапуска
(Updater.startup), затем фоновая задача проверяет релизы при старте и раз в CHECK_EVERY (автообновление — там же) и раз
в TICK смотрит, не пора ли перезапуститься: стоит новая версия, нет запросов HTTP IDLE секунд (updates.Activity —
внешний слой приложения) и ни одно соединение пула не занято. Перед os.execv пул закрывается.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import threading

import anyio
from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from .. import api, db, embed, lang, updates
from ..ocfl_store import ObjectStore
from ..settings import Settings, SettingsError, load_tokens
from . import problems, session

API = "/api/v1"
POOL_MIN, POOL_MAX = 1, 8
POOL_TIMEOUT = 5.0        # с; дольше ждать соединения нет смысла — ответ 503
OPEN_TIMEOUT = 10.0       # с; база не ответила при запуске — служба не стартует
SWEEP_EVERY = 3600.0      # с; подборки и загрузки старше суток удаляются при запуске и раз в час
KEEP_START = 15.0         # с после запуска до первого прохода embed.keep: сначала служба открывается и отвечает
KEEP_EVERY = 30.0         # с между проходами, когда всё посчитано: новая запись получает вектор за полминуты
KEEP_IDLE = 600.0         # с, когда считать нечем: канал выключен, нет файлов модели или в базе активна другая модель
log = logging.getLogger("amemo.service")


def create_app(settings: Settings, *, provider: embed.EmbeddingProvider | None = None,
               updater: updates.Updater | None = None, update_loop: bool = True,
               embed_loop: bool = True) -> Starlette:
    """Служба HTTP по настройкам; provider — эмбеддинги для смыслового канала (None — по AMEMO_BACKEND); updater —
    обновления (None — по виду установки этого процесса), update_loop — фоновая проверка и перезапуск (тесты
    выключают её, чтобы звать шаги сами); embed_loop — фоновое досчитывание векторов (embed.keep)."""
    if not settings.dsn:
        raise SettingsError("база не задана: задайте AMEMO_DSN (или dsn в amemo.toml) и выполните amemo init")
    tokens = load_tokens(settings.tokens) if settings.tokens is not None else None

    from . import mcp as mcp_

    endpoint = mcp_.HttpMcp(settings)        # /mcp: менеджер сессий SDK — в lifespan
    updater = updater if updater is not None else updates.Updater(settings)
    activity = updates.Activity()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        pool = ConnectionPool(settings.dsn, kwargs={"autocommit": True}, min_size=POOL_MIN, max_size=POOL_MAX,
                              open=False, timeout=POOL_TIMEOUT, check=ConnectionPool.check_connection,
                              name="amemo")
        app.state.base_id = await anyio.to_thread.run_sync(_open, pool)
        app.state.pool = pool
        updater.before_restart = pool.close
        updater.idle = lambda: activity.quiet_for(updates.IDLE) and updates.pool_idle(pool)
        updater.free = lambda: activity.inflight == 0 and updates.pool_idle(pool)
        updater.before_install = lambda: _snapshot_ui(app, updater)
        try:
            await anyio.to_thread.run_sync(_sweep, settings)
            await anyio.to_thread.run_sync(_updates_startup, updater)
            await anyio.to_thread.run_sync(_preload, app)
            async with endpoint.run(app), anyio.create_task_group() as tasks:
                tasks.start_soon(_sweeper, settings)
                if update_loop:
                    tasks.start_soon(_updates, updater)
                if embed_loop:
                    tasks.start_soon(_embeddings, app)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            await anyio.to_thread.run_sync(pool.close)

    from . import http_v1, ui       # маршруты зовут connection() отсюда: импорт — после того, как модуль готов

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), *http_v1.ROUTES,
                Route(session.MCP, endpoint, methods=["GET", "POST", "DELETE"]), *ui.ROUTES],
        middleware=[Middleware(updates.Track, activity=activity),
                    Middleware(session.Guard, settings=settings, secret=secrets.token_urlsafe(32), tokens=tokens)],
        exception_handlers=problems.HANDLERS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider = provider
    app.state.provider_lock = threading.Lock()
    app.state.provider_chosen = provider is not None
    app.state.store = ObjectStore(settings.data)      # только чтение копий: корень создаёт первая запись файла
    app.state.base_id = None
    app.state.ui_root = None                          # lifespan запоминает корень сборки /ui (_preload)
    app.state.mcp = endpoint                          # /mcp: его runtime (задачи клиентов без сессии)
    app.state.updates = updater
    app.state.activity = activity
    return app


def _open(pool: ConnectionPool) -> str | None:
    """Открыть пул и подготовить базу; base_id — для журнала запросов (база не меняет его, пока служба жива)."""
    pool.open(wait=True, timeout=OPEN_TIMEOUT)
    try:
        with pool.connection() as conn:
            db.prepare(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'base_id'").fetchone()
    except BaseException:
        pool.close()
        raise
    return row[0] if row else None


def _sweep(settings: Settings) -> None:
    """Подборки и загрузки старше суток — долой (api.bundle_sweep); сбой диска не роняет службу."""
    try:
        removed = api.bundle_sweep(settings.state)
    except OSError as e:
        log.warning("bundles in %s are not cleaned up: %s", settings.state, e)
        removed = 0
    if removed:
        log.info("removed bundles and uploads older than a day: %d", removed)
    try:
        parts = api.object_upload_sweep(settings.docs)
    except OSError as e:
        log.warning("temporary upload files in the documents folder are not cleaned up: %s", e)
        return
    if parts:
        log.info("removed abandoned temporary upload files in the documents folder: %d", parts)


async def _sweeper(settings: Settings) -> None:
    while True:
        await anyio.sleep(SWEEP_EVERY)
        await anyio.to_thread.run_sync(_sweep, settings)


async def _embeddings(app: Starlette) -> None:
    """Смысловой поиск включён всегда: проходы embed.keep, пока служба жива."""
    await anyio.sleep(KEEP_START)
    while True:
        delay = await anyio.to_thread.run_sync(_keep_embeddings, app, abandon_on_cancel=True)
        await anyio.sleep(delay)


def _keep_embeddings(app) -> float:
    """Один проход embed.keep; ответ — через сколько секунд следующий: сразу, пока остаются документы без вектора,
    KEEP_EVERY, когда всё посчитано, KEEP_IDLE, когда считать нечем. Сбой базы или модели службу не роняет."""
    try:
        with app.state.pool.connection() as conn:
            kept = embed.keep(conn, choose_provider(app.state))
    except Exception as e:                                # noqa: BLE001 — фоновая задача переживает любой сбой прохода
        log.warning("semantic search vectors are not caught up: %s: %s", type(e).__name__, e)
        return KEEP_EVERY
    if kept["embedded"]:
        log.info("semantic search: vectors computed %d, left %d (%s)",
                 kept["embedded"], kept["missing"], kept["state"])
    if kept["state"] in ("off", "unavailable", "foreign"):
        return KEEP_IDLE
    return 0.0 if kept["embedded"] and kept["missing"] else KEEP_EVERY


def _preload(app: Starlette) -> None:
    """Весь код пакета — в память при старте (updates.preload), корень сборки /ui — запомнить: обновление заменит
    файлы на диске, а до перезапуска служба работает на прежнем коде и отдаёт прежнюю сборку."""
    from . import problems as problems_, ui

    for name, why in updates.preload():
        log.warning("the module %s was not imported ahead: %s", name, why)
    try:
        app.state.ui_root = ui.static_root()
    except problems_.Problem:
        app.state.ui_root = None              # веба нет: /ui ответит 404 с подсказкой, как раньше


def _snapshot_ui(app: Starlette, updater: updates.Updater) -> None:
    """Перед установкой на место: копия текущей сборки /ui в AMEMO_STATE/updates/ui-<версия> — её служба и отдаёт
    до перезапуска (файлы пакета заменит новая версия)."""
    from . import ui

    root = app.state.ui_root
    if root is None:
        return
    target = updater.root / updates.VENVS / f"ui-{updater.current}"
    if target.is_symlink():
        return
    app.state.ui_root = ui.snapshot(root, target)


def _updates_startup(updater: updates.Updater) -> None:
    try:
        updater.startup()
    except OSError as e:                         # AMEMO_STATE не пишется — служба работает и без обновлений
        log.warning("the update state in %s is not written: %s", updater.root, e)


async def _updates(updater: updates.Updater) -> None:
    """Проверка релизов при старте и раз в CHECK_EVERY (с автообновлением), раз в TICK — перезапуск в простое.
    Сбой шага не роняет службу: он в журнале, следующий — по расписанию."""
    next_check = 0.0
    while True:
        try:
            if updater.clock() >= next_check:
                next_check = updater.clock() + updates.CHECK_EVERY
                await anyio.to_thread.run_sync(updater.scheduled, abandon_on_cancel=True)
            await anyio.to_thread.run_sync(updater.watch, updater.idle, updater.free, abandon_on_cancel=True)
        except Exception:  # noqa: BLE001
            log.exception("updates: the step failed")
        await anyio.sleep(updates.TICK)


@contextlib.contextmanager
def connection(request: Request):
    """Соединение из пула службы на время запроса: with connection(request) as conn. На то же время — язык вызова
    по Accept-Language (amemo.lang.using): подписи ответов операций api — на нём, с заменой на другой слот."""
    language = lang.from_header(request.headers.get("accept-language"))
    with request.app.state.pool.connection() as conn, lang.using(language):
        yield conn


def provider(request: Request) -> embed.EmbeddingProvider | None:
    """provider смыслового канала: явный из create_app или один на запуск службы по AMEMO_BACKEND — выбирается при
    первом поиске под замком, поэтому модель fastembed грузится один раз, а не на каждый запрос. AMEMO_BACKEND=none
    рецепты проверяют сами при каждом вызове; неизвестный AMEMO_BACKEND — None, рецепт назовёт причину."""
    return choose_provider(request.app.state)


def choose_provider(state) -> embed.EmbeddingProvider | None:
    """provider по состоянию приложения (app.state): то же, что provider(request), — для /mcp, у которого запроса
    Starlette в руках нет."""
    if not state.provider_chosen:
        with state.provider_lock:
            if not state.provider_chosen:
                try:
                    state.provider = embed.get_provider(None)
                except embed.EmbeddingError:
                    state.provider = None
                state.provider_chosen = True
    return state.provider


def health(request: Request) -> PlainTextResponse:
    with connection(request) as conn:            # база недоступна — 503, а не «ok»
        conn.execute("SELECT 1")
    return PlainTextResponse("ok")
