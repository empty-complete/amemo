import { i18n } from "../app/i18n";
import { russian } from "./issues";
import type { components } from "./schema";

type Problem = components["schemas"]["Problem"];
export type CheckIssue = components["schemas"]["CheckIssue"];

/** Заголовок ошибки на языке интерфейса (словарь error.*): по code ответа problem+json — точнее, чем по статусу
 * (503 бывает и «нет службы», и «не уложилась»); кода в словаре нет — по статусу. Ответ вовсе без code — не от
 * службы amemo: его собственный заголовок (fallback) точнее общего по статусу. */
export function problemTitle(code: string | undefined, status: number, fallback?: string): string {
  const byCode = code ? i18n.t(`error.code.${code}`, { defaultValue: "" }) : "";
  const byStatus = i18n.t(`error.status.${status}`, { defaultValue: "" });
  return byCode || (code ? byStatus || fallback : fallback || byStatus) || i18n.t("error.unknown");
}

/** Ошибка службы в формате problem+json (RFC 9457), разобранная для экрана. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail?: string,
    public code?: string,
    public issues: CheckIssue[] = [],
  ) {
    super(detail ?? title);
    this.name = "ApiError";
  }
}

export async function problemFromResponse(res: Response): Promise<ApiError> {
  const type = res.headers.get("content-type") ?? "";
  if (type.includes("json")) {
    try {
      const p = (await res.json()) as Partial<Problem>;
      return new ApiError(
        p.status ?? res.status,
        problemTitle(p.code, p.status ?? res.status, p.title),
        p.detail,
        p.code,
        // у check_rejected рядом с ошибками — предупреждения (private_removed и др.): человек видит всё сразу
        [...(p.issues ?? []), ...(p.warnings ?? [])],
      );
    } catch {
      // тело не JSON: остаётся ошибка по статусу
    }
  }
  return new ApiError(res.status, problemTitle(undefined, res.status));
}

/** Сеть недоступна вовсе (служба не запущена). */
export function offlineError(cause: unknown): ApiError {
  return new ApiError(0, "Нет связи со службой amemo", cause instanceof Error ? cause.message : undefined);
}

/** Текст ошибки службы для экрана. Подробности служба пишет по-английски — их читают агенты и журналы, — поэтому
 * в русском интерфейсе впереди идёт заголовок по коду на русском, за ним подробность службы как есть. Подробность
 * уже по-русски (служба прежней версии) или интерфейс английский — она одна. */
export function problemText(err: ApiError): string {
  if (!err.detail) return err.title;
  if (i18n.language !== "ru" || russian(err.detail)) return err.detail;
  return `${err.title}: ${err.detail}`;
}

/** Текст ошибки для человека: что случилось; для обрыва связи — подсказка, что делать. */
export function errorText(err: unknown, offline: string): string {
  if (err instanceof ApiError) return err.status === 0 ? offline : problemText(err);
  return offline;
}
