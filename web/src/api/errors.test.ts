import { ApiError, problemFromResponse, problemText } from "./errors";

test("problem+json становится ApiError с кодом и замечаниями", async () => {
  const res = new Response(
    JSON.stringify({
      type: "about:blank",
      title: "Отклонено",
      status: 422,
      code: "level1",
      issues: [{ code: "has_label", severity: "violation", message: "Нет подписи" }],
    }),
    { status: 422, headers: { "content-type": "application/problem+json" } },
  );
  const err = await problemFromResponse(res);
  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(422);
  expect(err.code).toBe("level1");
  expect(err.issues[0]?.message).toBe("Нет подписи");
});

test("ответ без тела становится ApiError по статусу", async () => {
  const err = await problemFromResponse(new Response(null, { status: 503 }));
  expect(err.status).toBe(503);
  expect(err.title).toBe("Служба недоступна");
});

test("check_rejected: замечания и предупреждения вместе; 413 — словами", async () => {
  const body = {
    title: "Отклонено проверкой",
    status: 422,
    code: "check_rejected",
    issues: [{ code: "schema", severity: "violation", message: "Не по схеме", ref: "@n0", path: "label" }],
    warnings: [
      { code: "private_removed", severity: "warning", message: "Вырезано скрытое", ref: "@n0", path: "body" },
    ],
  };
  const res = new Response(JSON.stringify(body), {
    status: 422,
    headers: { "content-type": "application/problem+json" },
  });
  const err = await problemFromResponse(res);
  expect(err.issues.map((i) => i.code)).toEqual(["schema", "private_removed"]);
  expect((await problemFromResponse(new Response("", { status: 413 }))).title).toBe("Слишком большой запрос");
});

test("заголовок по коду точнее статуса: 503 timeout, 507 quota_exceeded, 408", async () => {
  const pj = (status: number, code: string) =>
    new Response(JSON.stringify({ title: "x", status, code, detail: "подробно" }), {
      status,
      headers: { "content-type": "application/problem+json" },
    });
  expect((await problemFromResponse(pj(503, "timeout"))).title).toMatch(/не уложилась в 15 с/);
  expect((await problemFromResponse(pj(507, "quota_exceeded"))).title).toMatch(/исчерпано/);
  const e = await problemFromResponse(new Response("", { status: 408 }));
  expect(e.title).toBe("Служба не дождалась запроса");
});

test("заголовок — по коду на языке интерфейса; английская подробность идёт следом, русская — одна", async () => {
  const problem = (body: object, status: number) =>
    problemFromResponse(
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/problem+json" } }),
    );
  const english = await problem(
    { title: "Not found", status: 404, code: "not_found", detail: "no record with the key wind_old" },
    404,
  );
  expect(english.title).toBe("Не найдено");
  expect(problemText(english)).toBe("Не найдено: no record with the key wind_old");
  const russian = await problem(
    { status: 404, code: "not_found", detail: "нет записи с ключом wind_old" },
    404,
  );
  expect(problemText(russian)).toBe("нет записи с ключом wind_old");
  const bare = await problem({ status: 409, code: "folder_exists" }, 409);
  expect(problemText(bare)).toBe("Папка уже есть");
  const unknown = await problem({ title: "Teapot", status: 418, code: "brand_new" }, 418);
  expect(unknown.title).toBe("Teapot");
});
