"""Ошибки службы в application/problem+json (RFC 9457) по таблице разд. 3.2 спецификации подпроекта 4.

Тело: type, title, status, detail, code и, где есть, расширения (issues, changeset_id и warnings у check_rejected;
by и at у review_closed; missing…); расширение не заменяет пять основных полей. Исключения библиотеки — наследники
ValueError, поэтому перевод идёт от частного к общему одной цепочкой (to_problem): отказ проверки (api.CheckRejected)
и RecordNotFound раньше RecordError (api.Missing — подборки нет — тоже 404; api.Ambiguous — текст указал не на
одну запись — 409 ambiguous с candidates: путей с текстом вместо ссылки у службы нет, перевод — для CLI и MCP),
коды ReviewError, FolderError, MineError, TaskError, ObjectError и UpdateError (409 с кодом отказа обновления) раньше «прочего», EmbeddingError (503) раньше прочего ValueError —
последней ступени перед 500: так библиотека отвечает на неверный ввод, и ответ — 400 с её сообщением.
Необработанная ошибка — 500 internal: подробности уходят в журнал службы (logger amemo.service), в ответе их нет.
Недоступная база — 503 без строки соединения: в тексте ошибки psycopg бывают адрес и порт сервера; значение,
которое база не приняла (psycopg.DataError), — 400 тоже без её текста. Хранилище копий без корня (том не подключён) — 503
store_unavailable, копии версии нет — 404; путь на диске — только в журнале. Тяжёлый запрос, который база оборвала
по statement_timeout (psycopg.errors.QueryCanceled — подкласс OperationalError, поэтому раньше него), — 503 timeout;
подборки владельца сверх предела места (api.QuotaExceeded) — 507 quota_exceeded.
У каждого ответа-ошибки — X-Content-Type-Options: nosniff.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime

import psycopg
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import api, bundle, db, embed, mine, objects, records, review, tasks, updates, vocab
from ..canon import CanonError
from ..check import CheckError
from ..folders import FolderError
from ..packs import PackError

MEDIA_TYPE = "application/problem+json"
log = logging.getLogger("amemo.service")

# заголовок — короткая фраза по-английски, как и подробность: их читают агенты и журналы. Экран показывает свой
# заголовок на языке интерфейса по code и статусу (веб — словарь error.*), а этот — только когда своего нет
TITLES = {
    400: "Bad request", 401: "Sign-in required", 403: "Forbidden", 404: "Not found", 405: "Method not allowed",
    408: "The request body did not arrive", 409: "Conflict", 413: "Too much data", 415: "Unsupported media type",
    416: "The range is outside the file", 421: "Foreign service address", 422: "Rejected by the check",
    500: "Internal service error", 501: "Not done yet", 503: "The service is unavailable", 507: "No space left",
}
CODE_TITLES = {"read_only": "Read only", "review_closed": "Already resolved", "bad_origin": "Foreign request origin",
               "forbidden": "Only for a person", "check_rejected": "Rejected by the check",
               "folder_exists": "The folder already exists", "folder_not_empty": "The folder is not empty",
               "semantic_unavailable": "Semantic search is unavailable",
               "db_unavailable": "The database is unavailable",
               "store_unavailable": "The file store is unavailable", "timeout": "The request took too long",
               "body_timeout": "The request body did not arrive", "quota_exceeded": "No space left for bundles",
               "ambiguous": "Ambiguous", "template_exists": "The kind code is taken",
               "template_in_use": "The kind has records", "template_invalid": "The kind did not pass the check",
               "task_closed": "The task is already closed", "task_claimed": "Another agent holds the task",
               "update_unsupported": "Updated elsewhere", "backup_required": "A backup is required",
               "no_update": "No updates", "update_busy": "An update is already being installed",
               "auto_update_off": "Automatic updates are switched off in the settings"}
RESERVED = frozenset({"type", "title", "status", "code", "detail"})       # расширения их не заменяют
REVIEW = {"closed": (409, "review_closed"), "forbidden": (403, "review_forbidden"),
          "claimed": (403, "review_forbidden"), "stale": (400, "review_bad"), "bad_outcome": (400, "review_bad"),
          "invalid": (400, "review_bad"), "not_found": (404, "not_found")}
FOLDER = {"exists": (409, "folder_exists"), "not_empty": (409, "folder_not_empty"),
          "not_found": (404, "folder_not_found")}
MINE = {"exists": (409, "template_exists"), "in_use": (409, "template_in_use"), "invalid": (422, "template_invalid"),
        "not_found": (404, "not_found"), "forbidden": (403, "forbidden")}
OBJECT = {"too_large": (413, "too_large"), "not_found": (404, "not_found"), "invalid": (400, "bad_request"),
          "path_taken": (409, "object_path_taken"), "docs_unavailable": (503, "docs_unavailable")}
TASK = {"not_found": (404, "not_found"), "closed": (409, "task_closed"), "claimed": (409, "task_claimed"),
        "forbidden": (403, "forbidden"), "invalid": (400, "bad_request")}
HTTP = {404: "not_found", 405: "method_not_allowed"}


class Problem(Exception):
    """Ответ-ошибка, которую бросает сам слой службы: разбор запроса, 501, отказ проверки с issues."""

    def __init__(self, status: int, code: str, detail: str | None = None, *, headers: dict | None = None, **extra):
        super().__init__(detail or code)
        self.status, self.code, self.detail, self.headers, self.extra = status, code, detail, headers, extra


def body(status: int, code: str, detail: str | None = None, *, extra: Mapping | None = None) -> dict:
    """Тело problem+json: пять основных полей и расширения. Его же печатает CLI с --json при ошибке."""
    out = {"type": "about:blank", "title": CODE_TITLES.get(code) or TITLES.get(status, "Ошибка"), "status": status,
           "code": code}
    if detail:
        out["detail"] = detail
    # расширения — только то, что переводится в JSON: в деталях BundleError бывают произвольные значения
    extensions = {str(k): v for k, v in (extra or {}).items() if str(k) not in RESERVED}
    out.update(json.loads(json.dumps(extensions, ensure_ascii=False, default=str)))
    return out


def response(status: int, code: str, detail: str | None = None, *, headers: dict | None = None,
             extra: Mapping | None = None) -> JSONResponse:
    return JSONResponse(body(status, code, detail, extra=extra), status_code=status,
                        headers={"X-Content-Type-Options": "nosniff", **(headers or {})}, media_type=MEDIA_TYPE)


def to_problem(exc: BaseException) -> Problem:
    """Исключение → Problem по таблице разд. 3.2; порядок проверок — от частного к общему."""
    message = str(exc)
    if isinstance(exc, Problem):
        return exc
    if isinstance(exc, api.CheckRejected):
        return Problem(422, "check_rejected", message, issues=exc.issues, changeset_id=exc.changeset_id,
                       warnings=exc.warnings)
    if isinstance(exc, api.Forbidden):
        return Problem(403, "forbidden", message)
    if isinstance(exc, api.ReadOnly):
        return Problem(403, "read_only", message)
    if isinstance(exc, api.Ambiguous):
        return Problem(409, "ambiguous", message, candidates=exc.candidates)
    if isinstance(exc, (records.RecordNotFound, api.Missing)):
        return Problem(404, "not_found", message)
    if isinstance(exc, review.ReviewError):
        status, code = REVIEW.get(exc.code, (400, "review_bad"))
        if exc.code == "closed":
            at = exc.at.isoformat() if isinstance(exc.at, datetime) else None
            who = f"resolved by {exc.by}" if exc.by else "closed without a decision"
            return Problem(status, code, f"{message} ({who}{', ' + at if at else ''})", by=exc.by, at=at)
        return Problem(status, code, message)
    if isinstance(exc, FolderError):
        status, code = FOLDER.get(exc.code, (422, "folder_invalid"))
        return Problem(status, code, message)
    if isinstance(exc, mine.MineError):
        status, code = MINE.get(exc.code, (422, "template_invalid"))
        return Problem(status, code, message, **({"issues": exc.issues} if exc.issues else {}))
    if isinstance(exc, tasks.TaskError):
        status, code = TASK.get(exc.code, (400, "bad_request"))
        return Problem(status, code, message)
    if isinstance(exc, updates.UpdateError):
        return Problem(400 if exc.code == "bad_request" else 409, exc.code, message)
    if isinstance(exc, bundle.BundleError):
        problem = Problem(422, exc.code or "bundle_invalid", message)
        problem.extra = dict(exc.details)        # ключи деталей — любые, в том числе detail и headers
        return problem
    if isinstance(exc, objects.ObjectError):
        status, code = OBJECT.get(exc.code, (400, "bad_request"))
        return Problem(status, code, message)
    if isinstance(exc, (api.BadRequest, records.RecordError, CheckError, db.BaseNameError, vocab.VocabError,
                        CanonError, PackError)):
        return Problem(400, "bad_request", message)
    if isinstance(exc, embed.EmbeddingError):
        return Problem(503, "semantic_unavailable", f"semantic search is unavailable: {message}")
    if isinstance(exc, api.StoreUnavailable):
        return Problem(503, "store_unavailable", message)
    if isinstance(exc, api.QuotaExceeded):
        return Problem(507, "quota_exceeded", message)
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return Problem(503, "timeout", "the database query did not fit in the time allowed: narrow it (a smaller "
                       "limit or depth, a filter by project or statuses) or try again later")
    if isinstance(exc, psycopg.OperationalError):
        return Problem(503, "db_unavailable", "the database is unavailable — check that PostgreSQL is running and "
                       "AMEMO_DSN is right")
    if isinstance(exc, psycopg.DataError):
        return Problem(400, "bad_request", "the database does not accept a parameter value (for example a NUL "
                       "character in a string)")
    if isinstance(exc, HTTPException):
        code = HTTP.get(exc.status_code, f"http_{exc.status_code}")
        return Problem(exc.status_code, code, None, headers=exc.headers)
    if isinstance(exc, ValueError):          # последняя ступень перед 500: неверный ввод, который пропустила служба
        return Problem(400, "bad_request", message)
    return Problem(500, "internal", "internal service error; the details are in the service log")


async def handle(request: Request, exc: Exception) -> JSONResponse:
    problem = to_problem(exc)
    if problem.code == "db_unavailable":
        log.warning("%s %s: the database is unavailable: %s", request.method, request.url.path, exc)
    elif problem.code == "timeout":
        log.warning("%s %s: the database query was cut by statement_timeout", request.method, request.url.path)
    elif isinstance(exc, (api.StoreUnavailable, api.CopyMissing)):
        log.warning("%s %s: %s (%s)", request.method, request.url.path, exc, exc.where)
    elif problem.status >= 500 and problem is not exc and problem.code == "internal":
        log.error("%s %s: unhandled error", request.method, request.url.path, exc_info=exc)
    return response(problem.status, problem.code, problem.detail, headers=problem.headers, extra=problem.extra)


# классы, которые ловит ExceptionMiddleware (поиск по MRO исключения); Exception — последний рубеж
# (ServerErrorMiddleware): он отвечает 500 и передаёт исключение серверу дальше
HANDLED = (Problem, api.CheckRejected, api.Ambiguous, api.Forbidden, api.ReadOnly, api.Missing, api.BadRequest,
           records.RecordError, review.ReviewError, FolderError, mine.MineError, tasks.TaskError, bundle.BundleError,
           updates.UpdateError,
           objects.ObjectError, CheckError,
           db.BaseNameError, vocab.VocabError, CanonError, PackError, embed.EmbeddingError, api.StoreUnavailable,
           api.QuotaExceeded, psycopg.OperationalError, psycopg.DataError, HTTPException, ValueError, Exception)
HANDLERS = {cls: handle for cls in HANDLED}
