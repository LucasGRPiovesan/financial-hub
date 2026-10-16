import { expect, type Locator, type Page } from "@playwright/test";

/** The dev server compiles each lazy route chunk on first visit, which is slow when the host is busy. */
const CHUNK_LOAD = { timeout: 30_000 };

export class LedgerPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    // Client-side navigation: a hard load of a console route can be swallowed by the dev proxy (see docs/TESTING.md).
    await this.page.goto("/");
    await this.page.getByRole("link", { name: "Livro-razão", exact: true }).first().click();
    await expect(this.heading).toBeVisible(CHUNK_LOAD);
  }

  get heading(): Locator {
    return this.page.getByRole("heading", { level: 1, name: "Livro-razão" });
  }

  tab(name: "Balancete" | "Contas" | "Lançamentos"): Locator {
    return this.page.getByRole("tab", { name });
  }

  get trialBalance(): Locator {
    return this.page.getByRole("table", { name: /^Balancete, BRL$/ });
  }

  get entries(): Locator {
    return this.page.getByRole("table", { name: "Lançamentos do diário" });
  }

  async openFirstEntry(): Promise<void> {
    await this.tab("Lançamentos").click();
    await expect(this.entries).toBeVisible(CHUNK_LOAD);
    await this.entries.getByRole("link", { name: /^Abrir lançamento / }).first().click();
    await expect(this.page.getByRole("heading", { level: 1, name: "Lançamento contábil" })).toBeVisible(CHUNK_LOAD);
  }

  /** Waits for the entry page and its postings to render. */
  get postings(): Locator {
    return this.page.getByRole("table", { name: "Partidas do lançamento" });
  }
}
