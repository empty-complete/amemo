import { i18n } from "../app/i18n";

type IssueLike = { code?: string; message?: string };

/** Коды, у которых после «: » в тексте службы стоят данные (числа, путь, цитата) — их оставляем как есть. */
const TAIL = new Set([
  "schema",
  "record_error",
  "source_unresolved",
  "source_error",
  "number_not_found",
  "edition_filled",
  "quote_fuzzy",
  "conflict",
  "related",
]);

const count = (s: string, re: RegExp) => s.match(re)?.length ?? 0;
/** Текст уже по-русски: правило шаблона из пакета или служба прежней версии. */
const russian = (s: string) => count(s, /[А-Яа-яЁё]/g) > count(s, /[A-Za-z]/g);

/** Замечание автопроверки на языке интерфейса. Служба пишет замечания по-английски — так их читают агенты и
 * журналы; в русском интерфейсе показываем свой перевод по постоянному code. Перевода нет — текст службы. */
export function issueText(issue: IssueLike): string {
  const message = issue.message ?? "";
  if (i18n.language !== "ru" || !issue.code || russian(message)) return message;
  const own = i18n.t(`issue.code.${issue.code}`, { defaultValue: "" });
  if (!own) return message;
  const at = TAIL.has(issue.code) ? message.indexOf(": ") : -1;
  return at < 0 ? own : `${own}: ${message.slice(at + 2)}`;
}

/** Одна строка об отказе проверки: первое замечание — туда, где полей с подсветкой нет (окно быстрой записи). */
export function rejectedText(issues: IssueLike[]): string {
  return i18n.t("issue.rejected", { first: issues[0] ? issueText(issues[0]) : "" });
}
