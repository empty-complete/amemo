import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithApp } from "../../test/render";
import { ChangesPage } from "./ChangesPage";

test("лента по дням: сегодня сверху; каждая строка называет, кто и что сделал", async () => {
  await renderWithApp(<ChangesPage who="all" since="ever" />, "/changes");
  const today = await screen.findByRole("region", { name: "Сегодня" });
  // свои действия — «Вы …» (сессия в образцах — anna)
  expect(within(today).getByText("Вы записали ревизию 2")).toBeInTheDocument();
  expect(within(today).getByText("Вы добавили связь «см. также»")).toBeInTheDocument();
  expect(within(today).getByText("Вы приняли правку")).toBeInTheDocument();
  const yesterday = screen.getByRole("region", { name: "Вчера" });
  expect(within(yesterday).getByText("Записал: extractor (модель)")).toBeInTheDocument();
  expect(
    within(yesterday).getByText("Статус «Ждёт проверки» поставил: checker (модель)"),
  ).toBeInTheDocument();
  expect(within(yesterday).getByText("Связь снял: linker (модель)")).toBeInTheDocument();
});

test("фильтр «Агенты» — только модели", async () => {
  await renderWithApp(<ChangesPage who="model" since="ever" />, "/changes");
  await screen.findByRole("region", { name: "Вчера" });
  expect(screen.queryByText("anna")).not.toBeInTheDocument();
  expect(screen.getAllByText(/\(модель\)/).length).toBeGreaterThan(0);
});

test("пакет модели отменяется целиком: вопрос, затем отмена и итог", async () => {
  const user = userEvent.setup();
  await renderWithApp(<ChangesPage who="all" since="ever" />, "/changes");
  const yesterday = await screen.findByRole("region", { name: "Вчера" });
  // кнопка — одна, у пакета модели; у своих правок её нет; появляется, когда сессия загрузилась
  await user.click(await within(yesterday).findByRole("button", { name: "Отменить пакет" }));
  expect(screen.queryAllByRole("button", { name: "Отменить пакет" })).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Да, отменить" }));
  expect(
    await screen.findByText("Пакет отменён: выведено 1, возвращено 0, отклонено 0, не тронуто 0"),
  ).toBeInTheDocument();
});
