import { afterEach, expect, test } from "vitest";
import { i18n } from "../app/i18n";
import { issueText, rejectedText } from "./issues";

afterEach(async () => {
  await i18n.changeLanguage("ru");
});

test("русский интерфейс: перевод по коду, данные после двоеточия остаются", () => {
  expect(
    issueText({
      code: "pdf_quote_required",
      message: "A PDF source needs a verbatim quote of at least 20 characters",
    }),
  ).toBe("У источника из PDF нужна дословная цитата");
  expect(
    issueText({
      code: "number_not_found",
      message: "Numbers of the atom are not in the source fragment: 0.38, 12",
    }),
  ).toBe("Чисел записи нет во фрагменте источника: 0.38, 12");
});

test("текст правила из пакета и код без перевода — как у службы", () => {
  expect(issueText({ code: "template", message: "У заметки нет текста." })).toBe("У заметки нет текста.");
  expect(issueText({ code: "brand_new_code", message: "Something new" })).toBe("Something new");
  expect(issueText({ message: "No code at all" })).toBe("No code at all");
});

test("английский интерфейс: текст службы", async () => {
  await i18n.changeLanguage("en");
  const message = "A PDF source needs a page selector";
  expect(issueText({ code: "pdf_page_required", message })).toBe(message);
  expect(rejectedText([{ code: "pdf_page_required", message }])).toBe(
    `The check rejected the record: ${message}`,
  );
});

test("строка об отказе — с первым замечанием", () => {
  expect(
    rejectedText([{ code: "quote_not_found", message: "The quote is not in the text of the document" }]),
  ).toBe("Проверка отклонила запись: Цитаты нет в тексте документа");
});
