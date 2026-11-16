"""Локальные эмбеддинги текущих ревизий, покрытие модели и её безопасная смена.

Вектор принадлежит ревизии и модели. Синхронизация пересчитывает только текущие документы,
у которых изменился версионированный хэш поискового текста; вектора старых ревизий сохраняются.
fastembed — необязательная зависимость: её модуль и модель загружаются только при первом вызове.
Файлы модели лежат в models_dir() (AMEMO_MODELS, иначе FASTEMBED_CACHE_PATH, иначе ~/.local/share/amemo/models), а не
во временной папке fastembed по умолчанию: её чистит перезагрузка, и смысловой поиск молча ломался бы. Скачивает модель
только amemo embed enable (amemo/cli/commands/embed.py) — с разрешения человека.
"""
from __future__ import annotations

import hashlib
import importlib
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from pgvector import HalfVector
from pgvector.psycopg import register_vector

from .ids import uuid7
from .search import _FRESH_TEXT_HASH, SEARCH_TEXT_VERSION, _hash_params

Target = str


class EmbeddingError(ValueError):
    """Некорректный provider, отсутствующие данные или неполное покрытие модели."""


class EmbeddingProvider:
    """Базовый provider: единая проверка числа, размерности и конечности координат."""

    name: str
    version: str
    dims: int
    backend: str

    def embed(self, texts) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        vectors = list(self._embed(values))
        if len(vectors) != len(values):
            raise EmbeddingError(f"provider вернул число векторов {len(vectors)}, ожидалось {len(values)}")
        checked = []
        for i, vector in enumerate(vectors):
            try:
                coordinates = list(vector)
            except TypeError as e:
                raise EmbeddingError(f"вектор {i} не является последовательностью float") from e
            if len(coordinates) != self.dims:
                raise EmbeddingError(f"вектор {i}: размерность {len(coordinates)}, ожидалось {self.dims}")
            try:
                converted = [float(value) for value in coordinates]
            except (TypeError, ValueError, OverflowError) as e:
                raise EmbeddingError(f"вектор {i}: координаты должны быть конечными float") from e
            if not all(math.isfinite(value) for value in converted):
                raise EmbeddingError(f"вектор {i}: координаты должны быть конечными float")
            checked.append(converted)
        return checked

    def _embed(self, texts: list[str]):
        raise NotImplementedError


DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def models_dir(env=None) -> Path:
    """Папка файлов модели: AMEMO_MODELS, иначе FASTEMBED_CACHE_PATH (переменная самого fastembed), иначе
    ~/.local/share/amemo/models — рядом с хранилищем копий, а не во временной папке."""
    env = os.environ if env is None else env
    for var in ("AMEMO_MODELS", "FASTEMBED_CACHE_PATH"):
        value = env.get(var, "").strip()
        if value:
            return Path(value).expanduser()
    home = Path(env.get("HOME") or Path.home())
    return home / ".local" / "share" / "amemo" / "models"


class FastembedProvider(EmbeddingProvider):
    """Ленивый fastembed; без allow_download разрешены только файлы локального кэша. Модель создаётся под замком:
    служба зовёт провайдер из пула потоков, и первые параллельные поиски иначе создали бы её каждый свою.
    cache_dir не задан — models_dir()."""

    backend = "fastembed"

    def __init__(self, model_name: str = DEFAULT_MODEL, *,
                 version: str = "1", dims: int = 384, cache_dir: str | None = None,
                 allow_download: bool = False):
        self.name = model_name
        self.version = version
        self.dims = dims
        self.cache_dir = cache_dir if cache_dir is not None else str(models_dir())
        self.allow_download = allow_download
        self._model = None
        self._lock = threading.Lock()

    def _embed(self, texts: list[str]):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
        try:
            return [vector.tolist() if hasattr(vector, "tolist") else vector
                    for vector in self._model.embed(texts)]
        except ModuleNotFoundError:
            raise
        except Exception as e:              # lazy_load: файлы модели читаются при первом embed
            raise EmbeddingError(f"fastembed: модель {self.name} не считает вектора: {type(e).__name__}: {e}") from e

    def _load(self):
        """Модуль fastembed и модель; ошибка не запоминается — следующий вызов попробует снова."""
        try:
            module = importlib.import_module("fastembed")
        except ModuleNotFoundError as e:
            if e.name != "fastembed":
                raise
            raise EmbeddingError("fastembed не установлен: установите optional extra amemo[embed]") from e
        try:
            return module.TextEmbedding(
                model_name=self.name,
                cache_dir=self.cache_dir,
                lazy_load=True,
                local_files_only=not self.allow_download,
            )
        except ModuleNotFoundError:
            raise                       # чужая зависимость не установлена — ошибка окружения, как при импорте
        except Exception as e:          # нет файлов модели в кэше, повреждённая модель: provider недоступен
            raise EmbeddingError(f"fastembed: модель {self.name} не создаётся: {type(e).__name__}: {e}") from e


def get_provider(explicit: EmbeddingProvider | None = None) -> EmbeddingProvider | None:
    """Выбрать provider по AMEMO_BACKEND; none имеет приоритет даже над явным аргументом."""
    backend = os.environ.get("AMEMO_BACKEND", "fastembed").strip().lower()
    if backend == "none":
        return None
    if explicit is not None:
        return explicit
    if backend == "fastembed":
        return FastembedProvider()
    raise EmbeddingError(f"неизвестный AMEMO_BACKEND {backend!r}: нужен fastembed или none")


def text_hash(document: str) -> str:
    """Хэш именно поискового документа; версия текста входит и в метку, и в preimage."""
    preimage = f"amemo-search-text\0{SEARCH_TEXT_VERSION}\0{document}".encode("utf-8")
    return f"sha256-s{SEARCH_TEXT_VERSION}:{hashlib.sha256(preimage).hexdigest()}"


def _model_id(value: Any) -> uuid.UUID:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError) as e:
        raise EmbeddingError(f"model_id {value!r}: это не UUID") from e


def _model_row(conn: psycopg.Connection, model_id, *, lock: bool = False):
    row = conn.execute(
        "SELECT id, name, version, dims, backend, active, created_at FROM embedding_model "
        f"WHERE id = %s{' FOR UPDATE' if lock else ''}", (_model_id(model_id),)).fetchone()
    if row is None:
        raise EmbeddingError(f"модель эмбеддингов {model_id} не зарегистрирована")
    return row


def _model_json(row) -> dict:
    return {
        "id": str(row[0]), "name": row[1], "version": row[2], "dims": row[3],
        "backend": row[4], "active": row[5], "created_at": row[6].isoformat(),
    }


def register_model(conn: psycopg.Connection, provider: EmbeddingProvider) -> dict:
    """Идемпотентно зарегистрировать метаданные provider и адаптеры pgvector для соединения."""
    if not all(isinstance(getattr(provider, field, None), str) and getattr(provider, field).strip()
               for field in ("name", "version", "backend")):
        raise EmbeddingError("provider: name, version и backend должны быть непустыми строками")
    if isinstance(provider.dims, bool) or not isinstance(provider.dims, int) or provider.dims <= 0:
        raise EmbeddingError("provider.dims должен быть положительным целым числом")
    register_vector(conn)
    with conn.transaction():
        row = conn.execute(
            "SELECT id, name, version, dims, backend, active, created_at FROM embedding_model "
            "WHERE name = %s AND version = %s AND backend = %s",
            (provider.name, provider.version, provider.backend)).fetchone()
        if row is not None:
            if row[3] != provider.dims:
                raise EmbeddingError(f"модель уже зарегистрирована с размерностью {row[3]}, не {provider.dims}")
            return _model_json(row)
        model_id = uuid7()
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO embedding_model (id, name, version, dims, backend, active, created_at) "
            "VALUES (%s, %s, %s, %s, %s, false, %s)",
            (model_id, provider.name, provider.version, provider.dims, provider.backend, now))
    return _model_json(_model_row(conn, model_id))


def _documents_with_embeddings(conn: psycopg.Connection, model_id) -> list[tuple]:
    return conn.execute(
        "SELECT d.target, d.record_id, d.revision_id, d.document, e.text_hash, e.dims "
        "FROM search_document d LEFT JOIN embedding e ON e.target = d.target "
        "AND e.revision_id = d.revision_id AND e.model_id = %s "
        "ORDER BY d.target, d.record_id", (_model_id(model_id),)).fetchall()


def _current(rows: list[tuple], dims: int) -> tuple[list[tuple], list[tuple]]:
    fresh, stale = [], []
    for row in rows:
        expected = text_hash(row[3])
        (fresh if row[4] == expected and row[5] == dims else stale).append(row)
    return fresh, stale


def embedding_coverage(conn: psycopg.Connection, model_id) -> dict:
    """Покрытие именно текущих документов актуальным хэшем; пустая база покрыта полностью."""
    model = _model_row(conn, model_id)
    rows = _documents_with_embeddings(conn, model[0])
    fresh, stale = _current(rows, model[3])
    return {
        "model_id": str(model[0]), "total": len(rows), "covered": len(fresh),
        "missing": len(stale), "complete": not stale,
    }


def _write_embeddings(conn: psycopg.Connection, model, documents: list[tuple],
                      vectors: list[list[float]]) -> None:
    register_vector(conn)
    now = datetime.now(timezone.utc)
    with conn.transaction():
        for row, vector in zip(documents, vectors):
            conn.execute(
                "INSERT INTO embedding (target, revision_id, model_id, dims, text_hash, vec, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (target, revision_id, model_id) DO UPDATE SET "
                "dims = EXCLUDED.dims, text_hash = EXCLUDED.text_hash, vec = EXCLUDED.vec, "
                "created_at = EXCLUDED.created_at",
                (row[0], row[2], model[0], model[3], text_hash(row[3]), HalfVector(vector), now))


def _check_provider(model, provider: EmbeddingProvider) -> None:
    if provider.dims != model[3]:
        raise EmbeddingError(f"размерность provider {provider.dims} не совпадает с моделью {model[3]}")


def sync_embeddings(conn: psycopg.Connection, model_id, provider: EmbeddingProvider | None = None) -> dict:
    """Вычислить недостающие/устаревшие вектора текущих документов одним батчем."""
    model = _model_row(conn, model_id)
    rows = _documents_with_embeddings(conn, model[0])
    _, stale = _current(rows, model[3])
    if not stale:
        return {"embedded": 0, **embedding_coverage(conn, model[0])}
    selected = get_provider(provider)
    if selected is None:
        return {"embedded": 0, **embedding_coverage(conn, model[0])}
    _check_provider(model, selected)
    vectors = selected.embed([row[3] for row in stale])       # вся проверка — до первого INSERT
    _write_embeddings(conn, model, stale, vectors)
    return {"embedded": len(stale), **embedding_coverage(conn, model[0])}


def embed_revision(conn: psycopg.Connection, target: Target, revision_id, model_id,
                   provider: EmbeddingProvider | None = None) -> dict:
    """Вектор одной текущей ревизии; совпадающий хэш повторно provider не вызывает."""
    if target not in ("atom", "entity"):
        raise EmbeddingError(f"неизвестный вид записи {target!r}")
    model = _model_row(conn, model_id)
    row = conn.execute(
        "SELECT d.target, d.record_id, d.revision_id, d.document, e.text_hash, e.dims "
        "FROM search_document d LEFT JOIN embedding e ON e.target = d.target "
        "AND e.revision_id = d.revision_id AND e.model_id = %s "
        "WHERE d.target = %s AND d.revision_id = %s",
        (model[0], target, revision_id)).fetchone()
    if row is None:
        raise EmbeddingError(f"текущая поисковая ревизия {target}:{revision_id} не найдена")
    digest = text_hash(row[3])
    result = {"target": target, "revision_id": str(row[2]), "model_id": str(model[0]),
              "text_hash": digest, "embedded": False}
    if row[4] == digest and row[5] == model[3]:
        return result
    selected = get_provider(provider)
    if selected is None:
        return result
    _check_provider(model, selected)
    vector = selected.embed([row[3]])
    _write_embeddings(conn, model, [row], vector)
    result["embedded"] = True
    return result


def activate_model(conn: psycopg.Connection, model_id) -> dict:
    """Атомарно сделать модель единственной активной; неполное покрытие запрещает смену."""
    with conn.transaction():
        model = _model_row(conn, model_id, lock=True)
        coverage = embedding_coverage(conn, model[0])
        if not coverage["complete"]:
            raise EmbeddingError(f"нельзя активировать модель: не хватает {coverage['missing']} эмбеддингов")
        # UNIQUE на active проверяется после каждой строки: сначала снимаем старый флаг,
        # затем ставим новый. Оба UPDATE остаются неделимыми для других транзакций.
        conn.execute("UPDATE embedding_model SET active = false WHERE active AND id <> %s", (model[0],))
        conn.execute("UPDATE embedding_model SET active = true WHERE id = %s", (model[0],))
        model = _model_row(conn, model[0])
    return _model_json(model)


# --- смысловой поиск включён всегда: вектора досчитывает служба, а не человек командой ---

KEEP_BATCH = 32                         # документов за проход: модель не занимает процессор надолго
KEEP_LOCK = 0x616D656D00000002          # ключ pg_advisory_lock: один досчитывающий на базу («amemo» + 2)
_NOT_FRESH = f"(e.revision_id IS NULL OR e.dims <> %(dims)s OR NOT ({_FRESH_TEXT_HASH}))"
_STALE_FROM = ("FROM search_document d LEFT JOIN embedding e ON e.target = d.target "
               "AND e.revision_id = d.revision_id AND e.model_id = %(model_id)s "
               f"WHERE {_NOT_FRESH}")


def _stale_params(model) -> dict:
    return {"model_id": model[0], "dims": model[3], **_hash_params()}


def _stale_documents(conn: psycopg.Connection, model, limit: int) -> list[tuple]:
    """До limit текущих документов без свежего вектора модели. Отбор — в SQL: тексты остальных не читаются."""
    return conn.execute(
        f"SELECT d.target, d.record_id, d.revision_id, d.document, e.text_hash, e.dims {_STALE_FROM} "
        "ORDER BY d.target, d.record_id LIMIT %(limit)s", {**_stale_params(model), "limit": limit}).fetchall()


def _stale_count(conn: psycopg.Connection, model) -> int:
    return conn.execute(f"SELECT count(*) {_STALE_FROM}", _stale_params(model)).fetchone()[0]


def _kept(state: str, embedded: int = 0, missing: int = 0) -> dict:
    return {"state": state, "embedded": embedded, "missing": missing}


def keep(conn: psycopg.Connection, provider: EmbeddingProvider | None = None, *, batch: int = KEEP_BATCH) -> dict:
    """Держать смысловой поиск включённым без человека; служба зовёт это по расписанию (service/app.py).

    Активная модель есть — досчитать вектора новых и изменённых записей, до batch за вызов. Активной модели нет —
    взять модель provider, если она считает на месте (provider по умолчанию ничего не скачивает: нужны уже лежащие
    файлы модели), посчитать вектора пачками и, когда текущие документы покрыты целиком, сделать её активной — то же,
    что amemo embed enable, только без загрузки.

    Ответ — {"state", "embedded", "missing"}: embedded — посчитано за вызов, missing — сколько документов ещё без
    вектора. state: on — канал работает; enabling — вектора считаются, модель ещё не активна; off — AMEMO_BACKEND
    выключает канал или неизвестен; unavailable — модель не считает (нет пакета или файлов модели); foreign —
    в базе активна другая модель, её не трогаем; busy — считает другой процесс."""
    try:
        selected = get_provider(provider)
    except EmbeddingError:
        return _kept("off")
    if selected is None:
        return _kept("off")
    model = conn.execute("SELECT id, name, version, dims, backend, active, created_at FROM embedding_model "
                         "WHERE active").fetchone()
    described = (getattr(selected, "name", None), getattr(selected, "version", None),
                 getattr(selected, "dims", None), getattr(selected, "backend", None))
    if model is not None and tuple(model[1:5]) != described:
        return _kept("foreign")
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (KEEP_LOCK,)).fetchone()[0]:
        return _kept("busy")
    try:
        if model is None:
            try:
                selected.embed(["amemo"])                  # считает ли модель на месте; нет — канал остаётся выключен
            except EmbeddingError:
                return _kept("unavailable")
            model = _model_row(conn, register_model(conn, selected)["id"])
        stale = _stale_documents(conn, model, batch)
        if stale:
            try:
                vectors = selected.embed([row[3] for row in stale])
            except EmbeddingError:
                return _kept("unavailable", missing=_stale_count(conn, model))
            _write_embeddings(conn, model, stale, vectors)
        missing = _stale_count(conn, model)
        active = model[5]
        if not active and not missing:
            try:
                active = activate_model(conn, model[0])["active"]
            except EmbeddingError:                         # запись появилась между подсчётом и включением
                missing = _stale_count(conn, model)
        return _kept("on" if active else "enabling", len(stale), missing)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (KEEP_LOCK,))
