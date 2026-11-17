"""Коды выхода CLI (спецификация подпроекта 4, разд. 5.1) и подсказки «что сделать».

Таблица одна на службу и CLI: исключение сначала переводится в Problem службы (amemo/service/problems.to_problem —
те же ступени от частного к общему, что у HTTP), а CLI отображает статус и code ответа в код выхода. Так новая
ошибка библиотеки, получившая статус у службы, сразу получает и код CLI, и они не расходятся. Сверх таблицы службы —
только то, чего у HTTP нет: настройки, файлы на диске, бэкап, разбор аргументов.

  0 — успех; 1 — окружение (база недоступна или не подготовлена, нет хранилища, нет клиента PostgreSQL);
  2 — неверный вызов (аргументы, формат файла, право агента); 3 — неоднозначность (выведены кандидаты);
  4 — не найдено; 5 — отказ проверки (выведен отчёт); 6 — конфликт (пункт решён, папка занята, коллизия подборки).
"""
from __future__ import annotations

OK, ENV, USAGE, AMBIGUOUS, NOT_FOUND, REJECTED, CONFLICT = 0, 1, 2, 3, 4, 5, 6

# code ответа службы → код выхода; что не здесь — по статусу (BY_STATUS)
BY_CODE = {
    "ambiguous": AMBIGUOUS,
    "check_rejected": REJECTED,
    "review_closed": CONFLICT, "folder_exists": CONFLICT, "folder_not_empty": CONFLICT,
    "bundle_key_collision": CONFLICT, "bundle_pack_conflict": CONFLICT,
    "bundle_pack_missing": ENV,           # в базе нет пакета, на который опирается подборка: окружение получателя
    "db_unavailable": ENV, "store_unavailable": ENV, "semantic_unavailable": ENV, "timeout": ENV,
    "quota_exceeded": ENV, "internal": ENV,
    # обновления (amemo update): вид установки и бэкап — окружение; уже ставится, выключено настройкой — конфликт
    "update_unsupported": ENV, "backup_required": ENV, "update_check_failed": ENV,
    "update_busy": CONFLICT, "auto_update_off": CONFLICT,
}
BY_STATUS = {400: USAGE, 401: USAGE, 403: USAGE, 404: NOT_FOUND, 408: ENV, 409: CONFLICT, 413: USAGE, 415: USAGE,
             416: USAGE, 422: USAGE, 503: ENV, 507: ENV}

HINTS = {
    "db_unavailable": "set AMEMO_DSN or run amemo init",
    "db_auth": "check the user and the password in AMEMO_DSN",
    "no_dsn": "set AMEMO_DSN or run amemo init --dsn postgresql://…",
    "not_ready": "run amemo init",
    "store_unavailable": "check AMEMO_DATA and that the data volume is mounted (amemo doctor)",
    "not_found": "find the record: amemo search TEXT",
    "folder_not_found": "the folder tree: amemo folders tree",
    "ambiguous": "narrow the text or pass an atm:/ent: reference from the list",
    "check_rejected": "fix what the report names and send the batch again",
    "forbidden": "this action is for a person: run it in your own terminal, without an agent",
    "review_forbidden": "items free for your agent: amemo review claim",
    "review_closed": "open items: amemo review list --state queued",
    "folder_exists": "the folder tree: amemo folders tree",
    "folder_not_empty": "move the records and the nested folders first",
    "task_closed": "open tasks: amemo tasks list --state open",
    "task_claimed": "an agent holds the task; its author or a person can cancel it: amemo tasks cancel ID",
    "bundle_pack_missing": "load the missing packs into the database and try again",
    "bundle_key_collision": "record keys of the bundle are already taken in this database: see collisions",
    "bundle_pack_conflict": "own kinds of the bundle differ from own kinds of the database: see collisions",
    "semantic_unavailable": "semantic search: amemo embed status (the service turns it on itself; by hand: amemo "
                            "embed enable)",
    "backup": "the PostgreSQL 18 client goes to AMEMO_PG_BIN or PATH; a restore needs an empty database",
    "update_unsupported": "a Docker image and a working copy are updated by a person: the command is in the message",
    "backup_required": "the PostgreSQL 18 client goes to AMEMO_PG_BIN or PATH; then amemo update install",
    "update_check_failed": "check the network to api.github.com and pypi.org and repeat amemo update check",
    "auto_update_off": "auto_update = true in amemo.toml (or remove AMEMO_AUTO_UPDATE=false)",
    "tokens": "set AMEMO_TOKENS, the path to the tokens file, for example ~/.config/amemo/amemo-tokens.toml",
}


def exit_code(status: int, code: str) -> int:
    """Код выхода для ответа-ошибки службы (статус и code)."""
    if code in BY_CODE:
        return BY_CODE[code]
    if code.startswith("bundle_"):
        return USAGE                      # подборка не та: формат файла
    return BY_STATUS.get(status, ENV)


class CliError(Exception):
    """Ошибка CLI, которой нет у службы: настройки, окружение, файл. code — для --json и подсказки."""

    def __init__(self, message: str, *, exit: int, code: str, status: int | None = None, hint: str | None = None,
                 **extra):
        super().__init__(message)
        self.exit, self.code, self.status, self.extra = exit, code, status, extra
        self.hint = hint if hint is not None else HINTS.get(code)
