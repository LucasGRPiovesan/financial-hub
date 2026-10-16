import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatDateTime } from "../lib/datetime";
import { DEFAULT_LOCALE, getLocale, initLocale, LOCALE_STORAGE_KEY, setLocale } from "../lib/locale";
import { formatMoney } from "../lib/money";
import { json, mockApi, renderApp, resetApiState, ME } from "../test/harness";
import { hasMessage, messagesFor, translate, translateLoose } from "./translate";
import { LanguageMenu } from "../components/language-menu";
import { t, useI18n } from "./index";

const plain = (text: string) => text.replace(/\s/g, " ");

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  setLocale("en-US");
});

describe("translate", () => {
  it("fills {placeholders} and leaves unknown ones visible", () => {
    expect(translate("en-US", "members.removedDescription", { name: "Ana" })).toBe("Ana no longer has access.");
    expect(translate("pt-BR", "members.removedDescription", { name: "Ana" })).toBe("Ana não tem mais acesso.");
    expect(translate("en-US", "members.removeDescription", { name: "Ana" })).toContain("{email}");
  });

  it("picks plural forms with Intl.PluralRules per language", () => {
    expect(translate("en-US", "customers.loaded", { count: 1 })).toBe("1 customer loaded");
    expect(translate("en-US", "customers.loaded", { count: 0 })).toBe("0 customers loaded");
    expect(translate("en-US", "customers.loaded", { count: 1200 })).toBe("1,200 customers loaded");
    // Portuguese treats 0 as singular (CLDR "one" covers 0 and 1).
    expect(translate("pt-BR", "customers.loaded", { count: 0 })).toBe("0 cliente carregado");
    expect(translate("pt-BR", "customers.loaded", { count: 1 })).toBe("1 cliente carregado");
    expect(translate("pt-BR", "customers.loaded", { count: 2 })).toBe("2 clientes carregados");
    expect(translate("pt-BR", "customers.loaded", { count: 1200 })).toBe("1.200 clientes carregados");
  });

  it("translates runtime-built keys and falls back for unknown values", () => {
    expect(hasMessage("pt-BR", "roles.owner")).toBe(true);
    expect(translateLoose("pt-BR", "roles.owner", "owner")).toBe("Proprietário");
    expect(translateLoose("pt-BR", "roles.martian", "martian")).toBe("martian");
    expect(hasMessage("pt-BR", "roles")).toBe(false);
  });

  it("has the same keys in both languages (the compiler also enforces this)", () => {
    const keys = (node: unknown, prefix = ""): string[] =>
      typeof node === "object" && node !== null
        ? Object.entries(node).flatMap(([key, child]) => keys(child, `${prefix}${key}.`))
        : [prefix];
    expect(keys(messagesFor("pt-BR")).sort()).toEqual(keys(messagesFor("en-US")).sort());
  });
});

describe("locale store", () => {
  beforeEach(() => window.localStorage.clear());

  it("defaults to Portuguese and ignores the browser language", () => {
    vi.spyOn(window.navigator, "language", "get").mockReturnValue("en-US");
    initLocale();
    expect(DEFAULT_LOCALE).toBe("pt-BR");
    expect(getLocale()).toBe("pt-BR");
    expect(document.documentElement.lang).toBe("pt-BR");
    expect(t("nav.overview.label")).toBe("Visão geral");
  });

  it("persists the choice, restores it and updates <html lang>", () => {
    setLocale("en-US");
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en-US");
    expect(document.documentElement.lang).toBe("en-US");
    setLocale("pt-BR");
    initLocale();
    expect(getLocale()).toBe("pt-BR");
    window.localStorage.setItem(LOCALE_STORAGE_KEY, "en-US");
    initLocale();
    expect(getLocale()).toBe("en-US");
  });

  it("ignores an unknown stored value", () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, "fr-FR");
    initLocale();
    expect(getLocale()).toBe("pt-BR");
  });

  it("keeps working when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => initLocale()).not.toThrow();
    expect(getLocale()).toBe("pt-BR");
    expect(() => setLocale("en-US")).not.toThrow();
    expect(getLocale()).toBe("en-US");
    expect(document.documentElement.lang).toBe("en-US");
  });

  it("makes money and dates follow the language by default", () => {
    setLocale("pt-BR");
    expect(plain(formatMoney({ amount_minor: "123456", currency: "BRL" }))).toBe("R$ 1.234,56");
    expect(formatDateTime("2026-10-10T19:30:00Z", { timeZone: "America/Sao_Paulo" }).text).toContain("16:30:00");
    setLocale("en-US");
    expect(plain(formatMoney({ amount_minor: "123456", currency: "BRL" }))).toBe("R$1,234.56");
    expect(plain(formatDateTime("2026-10-10T19:30:00Z", { timeZone: "America/Sao_Paulo" }).text)).toBe(
      "Oct 10, 2026, 4:30:00 PM",
    );
  });
});

describe("language switch", () => {
  function Probe() {
    const { t: tr, locale } = useI18n();
    return (
      <p>
        {locale}:{tr("nav.projects.label")}
      </p>
    );
  }

  it("re-renders subscribed components instantly, without a reload", async () => {
    setLocale("pt-BR");
    const user = userEvent.setup();
    render(
      <>
        <LanguageMenu />
        <Probe />
      </>,
    );
    expect(screen.getByText("pt-BR:Projetos")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Idioma: Português" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "English" }));
    expect(await screen.findByText("en-US:Projects")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Language: English" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en-US");
  });

  it("switches the whole console from the operator menu and back", async () => {
    resetApiState();
    setLocale("pt-BR");
    mockApi({
      "GET /v1/auth/me": () => json(200, ME),
      "GET /health/live": () => json(200, { status: "live" }),
      "GET /health/ready": () => json(200, { status: "ready" }),
    });
    const user = userEvent.setup();
    renderApp("/projects");
    expect(await screen.findByRole("heading", { name: "Projetos" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Projetos · Financial Hub"));

    await user.click(screen.getByRole("button", { name: /^Menu do operador/ }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByRole("menuitemradio", { name: "English" }));
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();
    expect(document.title).toBe("Projects · Financial Hub");

    await user.click(screen.getByRole("button", { name: /^Operator menu/ }));
    await user.click(within(await screen.findByRole("menu")).getByRole("menuitemradio", { name: "Português" }));
    expect(await screen.findByRole("heading", { name: "Projetos" })).toBeInTheDocument();
  });
});
