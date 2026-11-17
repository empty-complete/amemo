"""Уровень 2 автопроверки — привязка к источнику (спецификация подпроекта 3, разд. 4).

Источники каждой записи пачки, разобранной уровнем 1:
1. разбор — путь → объект и версия (objects.resolve_sources по одному пути: одна ошибка не прячет другие; путь
   разбирается один раз на пакет, и ошибка по нему повторяется у каждого источника с этим путём);
   у источника с object проверяются версия и digest;
2. место — селекторы с цепочкой refined_by дают ожидаемую страницу, диапазон, локатор, лист и цитату (place);
   сверка — sources.judge по тексту версии (objects.extraction, кэш по digest); страницы скана, распознанные
   OCR, сверяются как остальные, с отметкой info quote_ocr и мягче по нечёткости; скан, который нечем распознать
   (нет языковых данных tesseract), — no_text или quote_not_found с объяснением;
3. числа атома ищутся во фрагментах — страницах, где сверка нашла место, и страницах диапазона селектора page;
4. редакция — если у источника атома её нет, а версия его объекта — источник ровно одной текущей сущности
   класса edition, она подставляется в тело.

У сущности ошибки разбора, версии и digest — violation, замечания о цитате и месте — warning; правила PDF и сверка
чисел к ней не применяются, а источник без цитаты и места (сам файл документа) не сверяется вовсе.

Итоги возвращаются по меткам записей, в базу уровень 2 их не пишет — это делает запись пакета. У атома итоги
fuzzy и not_found здесь тоже есть, но в строки source они не попадут: такой пакет отклоняется по violation.
У сущности fuzzy и not_found записываются вместе с предупреждением. Объекты при разборе путей пишутся сразу
(так устроен objects: копия файла — содержимое, а не утверждение). Prepared.payload заменяется телом
с разобранными источниками и подставленной редакцией.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .. import objects, sources
from ..ocfl_store import ObjectStore, StoreError
from . import numbers
from .level1 import Batch, Prepared
from .report import Report

LEVEL = 2
log = logging.getLogger(__name__)
# итоги, которые может вернуть уровень 2 (подмножество SourceVerdict; no_file сюда не входит: путь без файла —
# source_unresolved)
VERDICTS = frozenset({"ok", "moved", "fuzzy", "by_locator", "unresolved", "not_found", "no_text", "external",
                      "error"})
PREFIX = {"table": "табл. ", "clause": "п. ", "figure": "рис. "}
LOCATORS = ("table", "clause", "figure", "formula", "heading")
ONE_PAGE_OK = (".md", ".txt")              # одностраничное ok без цитаты принимается только у них
RANGE = re.compile(r"^\s*(\d+)\s*[-–—‒]\s*(\d+)\s*$")
MAX_RANGE = 50                             # страниц в диапазоне селектора page: больше — это не фрагмент
RETIRED = ("rejected", "deprecated", "superseded")
NO_OCR = "tesseract language data is missing or ocr = none (languages: amemo ocr install rus eng)"
FILE_KINDS = ("file",)


class _ExtractError(Exception):
    """Сбой извлечения текста файла: итог error и warning source_error."""


@dataclass
class SourceCheck:
    position: int
    verdict: str | None           # None — источник не сверялся (ошибка разбора, правило PDF, файл сущности)
    page: int | None
    anchor: str | None
    fragments: list[str] = field(default_factory=list)       # страницы для сверки чисел


@dataclass
class Place:
    page: int | None              # ожидаемая страница
    pages: list[int]              # страницы диапазона селектора page
    locator: str
    quote: str
    sheet: str | None

    @property
    def given(self) -> bool:
        """Место у источника сущности: цитата, локатор или лист. Одна страница без них — ссылка на сам документ."""
        return bool(self.quote or self.locator or self.sheet)


def _chain(selector):
    while isinstance(selector, dict):
        yield selector
        selector = selector.get("refined_by")


def place(selectors: list[dict] | None, quote: str | None) -> Place:
    """Ожидаемое место по селекторам и цепочкам refined_by (разд. 4.2)."""
    page, pages, locators, sheet, text_quote = None, [], [], None, ""
    for top in selectors or []:
        for s in _chain(top):
            kind, val = s.get("type"), str(s.get("value") or "")
            if kind in ("page", "slide"):                 # слайд презентации — её страница (extract.pptx_pages)
                if (p := sources.as_page(val)) is not None:
                    page = page or p
                elif (m := RANGE.match(val)) and 0 < int(m[1]) <= int(m[2]) < int(m[1]) + MAX_RANGE:
                    pages = pages or list(range(int(m[1]), int(m[2]) + 1))
            elif kind == "sheet":
                sheet = sheet or val
            elif kind in LOCATORS:
                if s.get("label"):
                    locators.append(s["label"])
                elif kind == "formula":
                    locators.append(f"формула ({val})")
                else:
                    locators.append(PREFIX.get(kind, "") + val)
            elif kind == "text_quote":
                text_quote = text_quote or val
    return Place(page, pages, "; ".join(x for x in locators if x.strip()), (quote or "").strip() or text_quote,
                 sheet)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().casefold()


def _sheet_page(pages: list[str], sheet: str) -> int | None:
    """Номер листа xlsx по имени: имя — первая строка страницы листа (разд. 9.1)."""
    want = _fold(sheet)
    for i, text in enumerate(pages):
        if _fold(text.split("\n", 1)[0]) == want:
            return i + 1
    return None


def check(conn: psycopg.Connection, store: ObjectStore, docs: Path | None, batch: Batch, report: Report, *,
          agent: str) -> dict[str, list[SourceCheck]]:
    """Уровень 2 над разобранной пачкой: замечания — в report, итоги — по меткам записей. StoreError — наружу.
    docs None — папка документов не настроена: источник с path — source_unresolved (спецификация 4, разд. 2)."""
    run = _Level2(conn, store, docs, report, agent)
    results: dict[str, list[SourceCheck]] = {}
    for item in batch.items:
        if item.ok and item.payload.get("sources"):
            results[item.label] = run.item(item)
    return results


def judge_source(conn: psycopg.Connection, store: ObjectStore, target: str, source: dict, position: int,
                 report: Report) -> SourceCheck:
    """Сверка одного разобранного источника (object, object_version, digest) с копией версии в хранилище — те же
    resolve и judge, что у check, без разбора путей, подстановки редакции и сверки чисел. Для импорта подборки:
    тело уже записано, итог — в его строку source. Копии файла нет — StoreError (вызывающий проверяет заранее)."""
    run = _Level2(conn, store, Path("."), report, None)
    where = f"sources[{position}]"
    src = run.resolve(source, where)
    if src is None:
        return SourceCheck(position, None, None, None)
    return run.judge(position, src, run.version(src), where, target == "atom")


class _Level2:
    def __init__(self, conn, store, docs, report: Report, agent: str):
        self.conn, self.store, self.docs, self.report, self.agent = conn, store, docs, report, agent
        self.texts: dict[tuple, tuple[sources.DocText | None, bool]] = {}   # текст версии, OCR недоступен
        # путь → поля разобранного источника (object, object_version, digest) или текст ошибки разбора — на пакет:
        # иначе sha256 файла считался бы заново для каждого источника с этим путём
        self.paths: dict[str, dict | str] = {}

    def add(self, severity: str, code: str, where: str, message: str, **details) -> None:
        self.report.add(LEVEL, severity, code, where, message, **details)

    def item(self, item: Prepared) -> list[SourceCheck]:
        is_atom = item.target == "atom"
        out: list[SourceCheck] = []
        resolved: list[dict] = []
        for i, source in enumerate(item.payload["sources"]):
            where = f"{item.label}: sources[{i}]"
            src = self.resolve(source, where)
            if src is None:
                resolved.append(source)
                out.append(SourceCheck(i, None, None, None))
                continue
            info = self.version(src)
            if is_atom and info[0] in FILE_KINDS and not src.get("edition"):
                self.fill_edition(src, where)
            resolved.append(src)
            out.append(self.judge(i, src, info, where, is_atom))
        item.payload = {**item.payload, "sources": resolved}
        if is_atom:
            self.numbers(item, out, resolved)
        return out

    # --- разбор ---

    def resolve(self, source: dict, where: str) -> dict | None:
        """Источник с object, object_version и digest или None (замечание уже записано)."""
        if not source.get("object"):
            path = source.get("path")
            if not path:
                return None
            if self.docs is None:            # служба без AMEMO_DOCS: путь не разобрать — ни файл, ни сайт, ни письмо
                self.add("violation", "source_unresolved", where, "The source is not resolved: the documents folder "
                         "is not set (AMEMO_DOCS), so the source path cannot be resolved", path=path)
                return None
            if path not in self.paths:
                try:
                    body = objects.resolve_sources(self.conn, self.store, self.docs, {"sources": [{"path": path}]},
                                                   agent=self.agent)
                except objects.ObjectError as e:
                    self.paths[path] = str(e).removeprefix("sources[0]: ")
                except StoreError as e:
                    raise StoreError(f"{where}: {str(e).removeprefix('sources[0]: ')}") from e
                else:
                    got = body["sources"][0]
                    self.paths[path] = {k: got[k] for k in ("object", "object_version", "digest") if k in got}
            found = self.paths[path]
            if isinstance(found, str):
                self.add("violation", "source_unresolved", where, f"The source is not resolved: {found}", path=path)
                return None
            return {**{k: v for k, v in source.items() if k != "digest"}, **found}
        object_id, n = objects.object_uuid(source["object"]), source.get("object_version")
        row = self.conn.execute("SELECT digest FROM object_version WHERE object_id = %s AND n = %s",
                                (object_id, n)).fetchone()
        if row is None:
            self.add("violation", "version_missing", where, f"The object {source['object']} has no version {n}",
                     object=source["object"], object_version=n)
            return None
        if source.get("digest") != row[0]:
            self.add("violation", "digest_mismatch", where,
                     f"digest {source.get('digest') or 'not given'} does not match version {n} of the object "
                     f"{source['object']}", digest=source.get("digest"), expected=row[0])
            return None
        return dict(source)

    def version(self, src: dict) -> tuple[str, str | None, str | None]:
        """(вид объекта, имя файла, media_type)."""
        return self.conn.execute("SELECT kind, original_name, media_type FROM object WHERE id = %s",
                                 (objects.object_uuid(src["object"]),)).fetchone()

    def fill_edition(self, src: dict, where: str) -> None:
        rows = self.conn.execute(
            "SELECT DISTINCT e.id FROM source s JOIN entity_current r ON r.id = s.entity_revision_id "
            "JOIN entity e ON e.id = r.entity_id WHERE e.entity_class = 'edition' AND e.status <> ALL(%s) "
            "AND s.object_id = %s AND s.object_version = %s",
            (list(RETIRED), objects.object_uuid(src["object"]), src["object_version"])).fetchall()
        if len(rows) == 1:
            src["edition"] = f"ent:{rows[0][0]}"
            self.add("info", "edition_filled", f"{where}.edition",
                     f"The edition is filled in from the file: {src['edition']}", edition=src["edition"])

    # --- место ---

    def text(self, src: dict, where: str) -> tuple[sources.DocText | None, bool]:
        """(текст версии или None, в файле есть сканы, а OCR недоступен); сбой извлечения — _ExtractError
        (итог error). StoreError и ошибки базы — наружу."""
        key = (src["object"], src["object_version"])
        if key not in self.texts:
            try:
                got = objects.extraction(self.conn, self.store, src["object"], src["object_version"])
            except FileNotFoundError as e:             # копии версии нет — хранилище не в порядке, а не данные
                raise StoreError(f"{where}: the store has no copy of the file ({e})") from e
            except (StoreError, psycopg.Error):
                raise
            except Exception as e:                     # сбой разбора файла (PyMuPDF, openpyxl …) — не ошибка данных
                log.exception("%s: text extraction failed for %s v%s", where, src["object"], src["object_version"])
                raise _ExtractError(f"{type(e).__name__}: {e}") from e
            doc = sources.DocText.of(got.pages, got.ocr, got.image) if got.pages is not None else None
            self.texts[key] = (doc, got.ocr_unavailable)
        return self.texts[key]

    def judge(self, i: int, src: dict, info, where: str, is_atom: bool) -> SourceCheck:
        kind, name, media_type = info
        p = place(src.get("selectors"), src.get("quote"))
        quote_sev = "violation" if is_atom else "warning"
        if kind not in FILE_KINDS:
            self.add("warning", "external_source", where,
                     "The source is a web page or a message: the place is not checked")
            return SourceCheck(i, "external", None, None)
        name = name or ""
        if is_atom and (media_type == "application/pdf" or name.lower().endswith(".pdf")):
            bad = False
            if not (p.page or p.pages):
                self.add("violation", "pdf_page_required", where, "A PDF source needs a page selector")
                bad = True
            if len(p.quote) < sources.MIN_QUOTE:
                self.add("violation", "pdf_quote_required", where,
                         f"A PDF source needs a verbatim quote of at least {sources.MIN_QUOTE} characters",
                         min_quote=sources.MIN_QUOTE)
                bad = True
            if bad:
                return SourceCheck(i, None, None, None)
        if not is_atom and not p.given:
            return SourceCheck(i, None, None, None)              # источник сущности-документа — сам файл
        try:
            doc, no_ocr = self.text(src, where)
        except _ExtractError as e:
            self.add("warning", "source_error", where, f"Text extraction failed: {e}",
                     error=type(e.__cause__).__name__)
            return SourceCheck(i, "error", None, None)
        if doc is None:
            if no_ocr:
                self.add("warning", "no_text", where, f"The file «{name}» has no text layer (a scan) and OCR "
                         f"is unavailable — {NO_OCR}: the place is not checked", ocr="unavailable")
            else:
                self.add("warning", "no_text", where, f"The file «{name}» has no text: the place is not checked")
            return SourceCheck(i, "no_text", None, None)
        v, expected = self.locate(doc, p, name)              # ошибка сверки — ошибка кода: она уходит наружу
        if v.page in doc.ocr:
            self.add("info", "quote_ocr", where, f"The place is checked against OCR text (page {v.page} is "
                     f"a scan without a text layer), not against a text layer", page=v.page)
        if v.verdict in ("ok", "moved", "by_locator"):
            if v.verdict == "moved":
                self.add("info", "page_moved", where, f"The quote is on page {v.page}, not {expected}",
                         page=v.page, expected=expected)
            found = sorted({v.page, *(n for n in p.pages if n <= len(doc.pages))})
            return SourceCheck(i, v.verdict, v.page, v.anchor or None, [doc.pages[n - 1] for n in found])
        if v.verdict == "fuzzy":
            self.add(quote_sev, "quote_fuzzy", f"{where}.quote",
                     f"The quote is found only approximately (similarity {v.score:.0f}%); "
                     f"the document has: «{v.anchor}»",
                     anchor=v.anchor, page=v.page, score=round(v.score, 1))
            return SourceCheck(i, "fuzzy", v.page, v.anchor)
        if v.verdict == "not_found":
            if p.page in doc.image:
                self.add(quote_sev, "quote_not_found", f"{where}.quote", f"Page {p.page} has no text "
                         f"layer (a scan) and OCR is unavailable — {NO_OCR}: nothing to check the quote against",
                         quote=p.quote, page=p.page, ocr="unavailable")
            else:
                self.add(quote_sev, "quote_not_found", f"{where}.quote", "The quote is not in the text of the document",
                         quote=p.quote)
            return SourceCheck(i, "not_found", None, None)
        self.add("warning", "place_unresolved", where, "No quote is given and the place is not found in the document")
        return SourceCheck(i, "unresolved", None, None)

    @staticmethod
    def locate(doc: sources.DocText, p: Place, name: str) -> tuple[sources.Verdict, int | None]:
        """Итог sources.judge с правилами уровня 2 (лист xlsx, одностраничное ok только у .md и .txt)
        и ожидаемая страница: селектора page или листа."""
        page = p.page
        if p.sheet:
            sheet = _sheet_page(doc.pages, p.sheet)
            if not p.quote:
                return (sources.Verdict("by_locator", sheet, p.sheet) if sheet else sources.Verdict("unresolved")), sheet
            page = sheet or page
        v = sources.judge(doc, p.quote, page, p.locator)
        if v.verdict == "ok" and not p.quote and not name.lower().endswith(ONE_PAGE_OK):
            return sources.Verdict("unresolved"), page       # docx без разрывов и книга из одного листа — не место
        return v, page

    # --- числа ---

    def numbers(self, item: Prepared, checks: list[SourceCheck], resolved: list[dict]) -> None:
        nums = numbers.atom_numbers(item.payload)
        if not nums:
            return
        fragments = [f for c in checks for f in c.fragments]
        if not any(c.fragments for c in checks):
            self.add("info", "numbers_unchecked", item.label,
                     "Nothing to check the numbers of the atom against: the sources have no fragment")
            return
        lost = numbers.missing(nums, fragments)
        derived = any(s.get("role") == "derived" for s in resolved)
        groups: dict[str, list] = {}
        for path, d, cond in lost:
            groups.setdefault("warning" if cond or derived else "violation", []).append({"path": path, "value": str(d)})
        for severity in ("violation", "warning"):
            if severity in groups:
                listed = ", ".join(n["value"] for n in groups[severity])
                self.add(severity, "number_not_found", item.label,
                         f"Numbers of the atom are not in the source fragment: {listed}", numbers=groups[severity])
