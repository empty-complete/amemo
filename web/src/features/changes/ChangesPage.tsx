import { ToggleButton, ToggleButtonGroup } from "@heroui/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { type ApiError, errorText, problemText } from "../../api/errors";
import { q } from "../../api/queries";
import { formatTime } from "../../ui/format";
import { RegionState } from "../../ui/RegionState";
import { recordPath } from "../records/RecordList";
import { eventText } from "./event";
import { groupByDay } from "./group";
import "./changes.css";

export type Who = "all" | "person" | "model";
export type Since = "7" | "30" | "ever";

const sinceIso = (s: Since) =>
  s === "ever" ? undefined : new Date(Date.now() - Number(s) * 86_400_000).toISOString();

function Filter<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: (readonly [T, string])[];
  onChange: (v: T) => void;
}) {
  return (
    <ToggleButtonGroup
      size="sm"
      selectionMode="single"
      disallowEmptySelection
      selectedKeys={new Set([value])}
      onSelectionChange={(keys) => {
        const k = [...keys][0];
        if (k != null) onChange(String(k) as T);
      }}
      aria-label={label}
    >
      {options.map(([k, l], i) => (
        <ToggleButton key={k} id={k}>
          {i > 0 && <ToggleButtonGroup.Separator />}
          {l}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}

/** Отменить пакет агента целиком: всё, что он записал, — одним действием и в два шага (вопрос, потом отмена). */
function RevertBatch({ changeset }: { changeset: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [step, setStep] = useState<"idle" | "ask" | "busy" | "done">("idle");
  const [note, setNote] = useState("");
  const run = async () => {
    setStep("busy");
    try {
      const res = await api.POST("/changesets/{id}/revert", {
        params: { path: { id: changeset } },
        body: {},
      });
      const r = res.data;
      if (r) {
        setNote(
          t("changes.reverted", {
            retired: r.retired.length,
            restored: r.restored.length,
            declined: r.declined.length,
            skipped: r.skipped.length,
          }),
        );
      }
      setStep("done");
      await qc.invalidateQueries();
    } catch (e) {
      setNote(problemText(e as ApiError));
      setStep("idle");
    }
  };
  if (step === "done" || (step === "idle" && note)) {
    return (
      <span className="ev__revert" role="status">
        {note}
      </span>
    );
  }
  if (step === "idle") {
    return (
      <button type="button" className="ev__revert" onClick={() => setStep("ask")}>
        {t("changes.revert")}
      </button>
    );
  }
  return (
    <span className="ev__revert">
      {t("changes.revert_ask")}{" "}
      <button type="button" disabled={step === "busy"} onClick={run}>
        {t("changes.revert_yes")}
      </button>{" "}
      <button type="button" disabled={step === "busy"} onClick={() => setStep("idle")}>
        {t("changes.revert_no")}
      </button>
    </span>
  );
}

/** Лента изменений базы по дням (разд. 5.8): кто и срок — фильтры в адресе. */
export function ChangesPage({ who = "all", since = "7" }: { who?: Who; since?: Since }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  // срок округляем до часа, чтобы ключ запроса не менялся на каждом рендере
  const from = useMemo(() => sinceIso(since)?.slice(0, 13).concat(":00:00Z"), [since]);
  const list = useQuery(q.changes({ who, since: from }));
  const me = useQuery(q.session()).data?.agent;
  const groups = useMemo(
    () =>
      list.data
        ? groupByDay(list.data.events, new Date(), i18n.language, {
            today: t("changes.today"),
            yesterday: t("changes.yesterday"),
          })
        : [],
    [list.data, i18n.language, t],
  );
  // пакет модели можно отменить целиком — кнопка у самого нового его события в ленте, у остальных её нет
  const person = me?.startsWith("person:") ?? false;
  const firstOf = useMemo(() => {
    const seen = new Set<string>();
    const first = new Set<string>();
    for (const g of groups)
      for (const e of g.events)
        if (e.changeset && e.agent?.startsWith("model:") && !seen.has(e.changeset)) {
          seen.add(e.changeset);
          first.add(e.id);
        }
    return first;
  }, [groups]);
  const set = (patch: { who?: Who; since?: Since }) =>
    navigate({ to: "/changes", search: (s: object) => ({ ...s, ...patch }) as never, replace: true });

  return (
    <section className="changes" aria-labelledby="ch-title">
      <div className="page-head">
        <h1 id="ch-title">{t("changes.title")}</h1>
      </div>
      <div className="changes__filters">
        <Filter
          label={t("changes.who")}
          value={who}
          options={(["all", "person", "model"] as const).map((w) => [w, t(`changes.${w}`)])}
          onChange={(v) => set({ who: v })}
        />
        <Filter
          label={t("changes.when")}
          value={since}
          options={[
            ["7", t("changes.d7")],
            ["30", t("changes.d30")],
            ["ever", t("changes.ever")],
          ]}
          onChange={(v) => set({ since: v })}
        />
      </div>
      {list.isPending ? (
        <RegionState state="loading" />
      ) : list.isError ? (
        <RegionState
          state="error"
          message={errorText(list.error, t("region.offline"))}
          retry={() => list.refetch()}
        />
      ) : groups.length === 0 ? (
        <RegionState state="empty" text={t("changes.empty")} />
      ) : (
        groups.map((g) => (
          <section key={g.key} className="day" aria-label={g.title}>
            <h2>{g.title}</h2>
            <ol>
              {g.events.map((e) => (
                <li key={e.id} className="ev">
                  <time dateTime={e.at}>{formatTime(e.at, i18n.language)}</time>
                  <span className="ev__what" data-model={e.agent?.startsWith("model:") || undefined}>
                    {eventText(e, t, i18n.language, me)}
                    {person && e.changeset && firstOf.has(e.id) && <RevertBatch changeset={e.changeset} />}
                  </span>
                  {e.ref ? (
                    <Link className="ev__rec" {...recordPath(e.ref)}>
                      {e.label ?? e.ref}
                    </Link>
                  ) : (
                    <span />
                  )}
                </li>
              ))}
            </ol>
          </section>
        ))
      )}
    </section>
  );
}
