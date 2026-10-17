# Frontend

The operator console (`frontend/`): React 19, TypeScript strict, TanStack Router/Query, Tailwind v4, Radix primitives,
lucide icons. Everything runs through Docker (`scripts/web-*.sh`); the API client is generated from `openapi.json` and
never edited by hand.

## Design language

**Direction: a ledger.** The console is a financial-operations tool, so it looks like one: calm, dense where the data is,
precise everywhere. An ink-navy sidebar holds the product chrome, the working area is a cool paper canvas, one
ultramarine marks actions, and one brass marks "you are here". No gradients, no illustration, no decorative cards.
The one memorable gesture is the double rule under a ruled total (`.ledger-rule`), borrowed from paper ledgers.

Tokens live in `src/index.css` (Tailwind `@theme`). Colours use `light-dark()`, so a single declaration serves both
themes.

### Typography

Geist (UI) and Geist Mono (ids, references, code), self-hosted variable fonts. Sentence case everywhere; no all-caps
labels, no "→" on links.

| Step | Size / line | Use |
|---|---|---|
| `2xs` | 11 / 16 px | Badges, fine print |
| `xs` | 12 / 17 px | Secondary lines, timestamps, metadata |
| `sm` | 13 / 20 px | Tables, forms, controls |
| `base` | 15 / 24 px | Body |
| `lg` | 18 / 26 px | Dialog and card titles |
| `xl` | 22 / 28 px | Page titles |
| `2xl` | 28 / 34 px | Large figures |

Headings use `tracking-tight` (-0.015em). Line length in prose stays under 80 characters.

### Colour (light / dark)

| Token | Light | Dark | Role |
|---|---|---|---|
| `canvas` | `#eef0ef` | `#0a0f17` | Page background |
| `surface` / `surface-sunken` | `#ffffff` / `#f6f7f6` | `#111826` / `#0d131e` | Cards, tables / wells |
| `line` / `line-strong` | `#dde1df` / `#b9c0bd` | `#243044` / `#3a4a63` | Hairlines |
| `control` | `#7a8491` | `#6f7f9a` | Form control edges (3:1) |
| `ink` / `ink-muted` / `ink-faint` | `#0e1726` / `#4a5568` / `#5f6b7d` | `#e8edf5` / `#9aa7bb` / `#8896ab` | Text |
| `chrome` | `#0e1726` | `#070b12` | Sidebar (ink in both themes) |
| `primary` | `#2b3fd8` | `#7b8cff` | Actions, links, focus ring |
| `brass` | `#a06d12` | `#e0b24f` | Current location |
| `danger` / `warning` / `success` / `info` | `#b42318` / `#8a5300` / `#0b6b4d` / `#1d4f91` | `#ff8a7e` / `#f2b45a` / `#5fd6a4` / `#7fb4f5` | Status (each has a `-soft` tint) |

Body text, muted text and every status colour meet WCAG AA against their surfaces in both themes (axe runs in the
test suite for every screen). Colour never carries meaning alone: statuses always have a text label, capability scope
also changes border style.

**Theme.** `prefers-color-scheme` by default; the top-bar Theme menu (Light / Dark / System) sets
`<html data-theme>` and persists to `localStorage` (`fh.theme`). `initTheme()` runs before the first render, so there is
no flash.

### Spacing, radius, density

4 px base unit via Tailwind's scale. Cards pad 20 px horizontally and 16 px vertically (`px-5 py-4`); the page gutter
grows from 16 px on phones to 40 px on 2xl screens. Tables use a 40 px header row and `py-2.5` cells with hairline
separators (no zebra); headers are sticky inside scrolling regions. Radii are tight
(3 / 5 / 7 px) and only floating layers (dialogs, menus) use 11 px. Shadows are a hairline plus a low ambient; only
overlays get a real lift.

### Layout (W9.12)

Content is fluid: `<main>` fills the viewport next to the fixed 15 rem sidebar, with gutters
`px-4 sm:px-6 xl:px-8 2xl:px-10` (the same in `BootScreen`). Tables and card grids stretch to the full width; only
prose (`PageHeader` description, empty-state copy, dialog forms) keeps a readable measure (`max-w-prose`, `max-w-lg`).
Sign-in is a split screen: product panel (lg and up, with a decorative balanced ledger entry on screens 800 px or
taller) and persona cards (initials avatar, email, role badge per project, `Continue as <name>` button, loading state
on the clicked card only). Four personas fit 1366x768 without page scroll.

### Motion

Defined once in `src/index.css`: `--ease-out-quart`, `--duration-fast` (150 ms), `--duration-base` (200 ms); the Tailwind
default transition uses the same pair. Only transform and opacity animate (no layout shift).

- Interactions: the `.motion-ui` utility (colour, border, shadow, opacity, transform) on buttons, rows, cards, inputs,
  tabs, menu items and nav links; buttons press 1 px.
- Overlays enter and leave: dialogs, menus and the nav sheet use `data-[state=open|closed]` animations, toasts slide out.
- Tab panels cross-fade; skeletons use a subtle shimmer (`.skeleton-shimmer`); the demo persona list and
  other first-render lists use `.stagger-in` (delay capped at 200 ms).
- Modules: `createRouter({ defaultViewTransition: { types } })` in `src/router.tsx` wraps path changes in the View
  Transitions API. Sidebar and top bar have their own `view-transition-name` (`vt-sidebar`, `vt-header`) and stay
  still; only `vt-page` (`<main>`) fades out in 120 ms and rises in 220 ms. Without the API, `.route-enter` plays the
  same rise as a CSS animation.
- `prefers-reduced-motion: reduce`: one global rule collapses every animation and transition, view-transition
  pseudo-elements are switched off, and the router skips `startViewTransition` entirely (no JS-driven motion).

### Data display rules

- Money goes through `<Money>`: minor units / strings only (never floats), right-aligned, tabular figures, currency
  code shown.
- Numbers in tables and counters use the `.figures` utility (`tabular-nums slashed-zero`).
- Identifiers (external references, ids, key prefixes, actions) are monospace.
- Status is a `Badge` with a text label (`Balanced`, `Not balanced`, `active`, role names).
- Timestamps are relative with the absolute time in a tooltip/`title` (`<RelativeTime>`, `<DateTime>`).
- Secrets and documents are never shown after submit: API keys appear once, CPF/CNPJ only as `document_masked`.

### Page anatomy

Every screen: `PageHeader` (title, one-line description, primary action, capability-scope label) then content. Each
data region has all four states: loading skeleton (`role="status"` with a label), empty (what it is and what to do),
error (the problem+json detail and "Try again"), and populated. Errors say what happened and how to fix it; they never
apologise.

Shell: sidebar with grouped nav and icons (collapses into a drawer below `lg`), top bar with breadcrumb, project
switcher, theme menu and operator menu (role badge, project, sign out). The Dialog scrolls inside the viewport and
keeps its action row pinned to the bottom, so no form is ever taller than the screen.

## Behaviour notes

- **Sign-in.** `GET /v1/auth/demo-operators` lists personas as cards ("Continue as <name>") with the banner "Demo mode -
  choose a persona; no password needed". `POST /v1/auth/demo-login` creates the real session (same `/auth/me` read, CSRF
  token and redirect as password login). A 404/network error falls back to the email/password form.
- **Permissions.** `features/console/permissions.ts` mirrors the backend role table to hide or disable controls
  (`GatedButton` explains why). The API remains the authority; 403s are mapped to toasts. Owner-related controls on
  Members are only active for owners.
- **Customers.** Cursor pagination ("Load more"), exact-match filter by `external_ref` or `email`, upsert by `PUT
  .../customers/{external_ref}` with client-side CPF/CNPJ check-digit validation, anonymize behind a type-the-reference
  confirmation. The upsert is deliberately not a TanStack mutation so the typed document is never held in the mutation
  cache; the field is cleared after every submit.
- **Payments.** `/payments` (list) and `/payments/$paymentId` (detail), both lazy routes. The API's list takes only
  `cursor`/`limit`, so the status/method/text filters narrow the pages already loaded (the UI says so); customer names
  come from one cached `GET /customers/{id}` per distinct id. Amounts stay integer minor units (`Money`, BigInt parsing
  in `features/payments/refund-amount.ts`, never floats). The refund dialog sends `POST .../refunds` with a fresh
  `Idempotency-Key` per attempt (the key is regenerated when the operator edits the request); the mutator forwards that
  one header only. `payments:refund` is owner and finance only, mirroring the backend. The API exposes no per-state
  history, so the detail "timeline" is what the record dates: creation, each refund, last update. Accessible names the
  e2e suite uses (pt-BR): table `Pagamentos`, link `Abrir cobrança <id>`, button `Estornar cobrança`, dialog `Estornar
  cobrança`, fields `Estorno parcial`, `Valor a estornar (R$)`, `Motivo (opcional)`, button `Confirmar estorno`, table
  `Estornos`.
- **Ledger and audit.** `/ledger` (heading `Livro-razão`) has URL-addressable tabs via `?tab=` (`trial`, `accounts`,
  `entries`; pt-BR tab names `Balancete`, `Contas`, `Lançamentos`) and `/ledger/entries/$entryId` shows one journal
  entry; `/audit` (`Auditoria`) lists audit records. All are lazy routes; money is integer minor units (`Money`), and the
  `[redacted]` marker the API sends is shown as a localized "hidden" value, never raw. The trial balance renders one
  table per currency with a `Balanceado` / out-of-balance badge; entry totals are computed from the postings and shown
  next to a "debits equal credits" badge. Text filters for accounts and audit narrow the pages already loaded (the UI
  says so); audit action and actor filters are sent to the API. Accessible names the e2e suite uses (pt-BR): tables
  `Balancete, BRL`, `Plano de contas`, `Lançamentos do diário`, `Partidas do lançamento`, `Registros de auditoria`;
  links `Abrir lançamento <id>`, `Abrir pagamento <id>`; buttons `Mostrar detalhes de <ação>`; search forms `Filtrar
  contas`, `Filtrar lançamentos`, `Filtrar registros de auditoria`. `ledger:read` and `audit:read` gate the screens; a
  viewer sees an explanation instead of data.
- **API keys.** The plaintext key is kept out of every query/mutation cache (`features/console/api.ts`).
- **Code splitting.** Screens are `React.lazy` routes (via `lazyRouteComponent`); the initial chunk is ~466 kB (was
  ~539 kB) with login and the shell eager.
- **Dev proxy.** `vite.config.ts` proxies `/v1`, `/health` and the legacy `^/api/` prefix. The `/api` rule is anchored
  with a slash so the SPA route `/api-keys` is not swallowed on a hard reload.

## Testing

`scripts/web-test.sh` (vitest + Testing Library + axe). Prefer role/label queries; the e2e suite relies on the same
accessible names, in the default language (pt-BR): `Continuar como <nome>`, `Projeto: <nome>`, `Menu do operador: <nome>`,
`Tema: <modo>`, `Idioma: <idioma>` (en-US: `Continue as`, `Project:`, `Operator menu:`, `Theme:`, `Language:`), `Sair`, nav
labels, dialog field labels. Rename one only together with the e2e suite; the e2e suite runs in pt-BR and has one journey
(`tests/language.spec.ts`) that switches to English and back.

## Internationalization

- **Languages.** Portuguese (`pt-BR`) is the default; English (`en-US`) is selectable at runtime. The browser language is
  deliberately not consulted. Code, identifiers and docs stay English; only UI copy is translated.
- **Layer (`src/i18n/`, no dependency).** `en-US.ts` is the canonical tree (`as const`) and defines `MessageKey`; `pt-BR.ts`
  is typed against its shape, so a key missing in one language fails the type check. `t(key, { name })` interpolates
  `{placeholders}`; a plural message is an object of CLDR forms (`one`/`other`) chosen with `Intl.PluralRules` from `count`.
  There is no Provider: the language lives in a small store (`lib/locale.ts`) that `useT()`, `useI18n()` and `useLocale()`
  subscribe to with `useSyncExternalStore`; non-React code uses the global `t()` / `tLoose()`.
- **Store.** `getLocale()` / `setLocale()` work anywhere; the choice is kept in `localStorage` (`fh.locale`, every access in
  try/catch, the app works without storage) and `<html lang>` follows it. `formatMoney` and `formatDateTime` default to the
  current language (`R$ 1.234,56` / `$1,234.56`); the time zone stays the browser's.
- **Switch.** `LanguageMenu` on the sign-in page and the radio group in the operator menu; instant, no reload.
- **Data vs. display.** Statuses, roles and scopes stay API values; they are translated at display time (`tLoose("status", value)`,
  falling back to the raw value). Problem+JSON errors map the known `code`/`type` to translated messages (`features/console/errors.ts`,
  `lib/api.ts`); the server `detail` is never the only text shown in pt-BR.
- **Adding copy.** Add the key to `en-US.ts` and `pt-BR.ts` (domain terms: lançamento, livro-razão, balancete, conta, cobrança,
  estorno, chave de API, integrante, cliente), then `t("section.key")`. Document titles are set per screen (`useDocumentTitle`).

### Polish notes (W9.10)

- Page headers carry no "Implemented" badge; only non-default scopes (Sandbox) and planned areas are marked
  (a dotted "Soon" tag in the sidebar, generated content so link names are unchanged).
- A hard load shows `BootScreen` (shell silhouette, motion-safe pulse) instead of a blank page.
- Recent activity text comes from `features/console/audit-activity.ts` (pure, table-tested), using only audit fields.

### Events explorer, billing and the Overview payments KPI (W9.17)

- **Events (`/events`, `/events/$eventId`).** Table named "Eventos, do mais recente ao mais antigo" (en: "Events, newest first"); each row has a link "Abrir evento {id}". Type filter: combobox "Tipo de evento", options read "Cobrança criada · payment.created" (humanized label plus raw type; unknown types show only the raw one). New events arrive over SSE: the list head query is invalidated by the stream, new rows get `animate-arrive` plus a "Novo" badge (the global reduced-motion rule removes the animation). Detail: "Envelope" (pretty JSON, "Copiar Envelope"), table "Entregas deste evento", ordered list "Causalidade" (oldest cause, this event with `aria-current="step"`, then descendants).
- **Billing (`/billing`, "Cobrança").** Section "Medição de uso": table "Métricas faturáveis", button "Nova métrica" (dialog "Nova métrica faturável", field "Campo do evento" disabled for count), per-row "Arquivar métrica {code}" (type the code to confirm). Gated by `metering:manage`; viewers see the buttons disabled with the reason. "Resumo de consumo" picks customer, metric and `[from, to)`; the total (`data-testid="usage-value"`) is the API decimal string formatted by string math only (`features/billing/decimal.ts`), never a float. The dashed "Planos, assinaturas e faturas — Em breve" card reserves space for invoices.
- **Overview KPI.** "Pagamentos (últimos 30 dias)": count of loaded payments created in the window and the captured BRL total, summed with BigInt over integer minor units (`features/payments/payments-kpi.ts`). It reuses the `usePayments` query (first page, 25 rows), so a "+" and a note appear when more pages exist.

### Webhooks, provider credentials and Settings (W9.18)

- **Webhooks (`/webhooks`, `/webhooks/$endpointId`).** Table "Endpoints de webhook" (en: "Webhook endpoints"); each row links "Abrir endpoint {url}". Button "Adicionar endpoint" opens the dialog "Adicionar endpoint de webhook" (fields "URL do endpoint", "Descrição", checkbox group "Tipos de evento"; "Todos os eventos" sends `["*"]`). The backend's URL message is shown inline under the URL field. The secret panel ("Guarde seu segredo de assinatura", read-only textbox "Segredo de assinatura", "Copiar segredo de assinatura", "Já guardei") cannot be dismissed until confirmed and the plaintext is held in component state only, never in the query or mutation cache. Gating: `webhooks:read` (owner, admin, finance) to see, `webhooks:manage` (owner, admin) to change.
- **Endpoint detail.** Actions "Editar", "Rotacionar segredo" (confirm; the previous secret keeps working 24 h and the new one is shown once), "Desativar"/"Ativar" (disable confirms), "Enviar evento de teste" (toast plus a "Ver evento" link to `/events/$eventId`). Table "Tentativas de entrega, mais recentes primeiro" with a "Atualizar" button; it also refetches every 8 s.
- **Resend.** In the event detail, webhook deliveries have "Reenviar entrega {id}" (`webhooks:manage`); the row turns "Pendente" optimistically and rolls back on error.
- **Credentials (`/integrations`).** "Atualizar credenciais de {conta}" opens a dialog that warns saving replaces the whole credential set. Secret inputs are password fields, never prefilled. "Gerar token" makes a 48-character alphanumeric Asaas webhook token with `crypto.getRandomValues` ("Copiar Token do webhook"); the read-only "URL do webhook" (`/v1/webhooks/asaas/{provider_account_id}`, same-origin API base) goes in the Asaas sandbox panel, the token in the `asaas-access-token` header.
- **Settings (`/settings`, "Configurações").** Cards Perfil, Preferências (radio groups "Idioma" and "Tema"), Projeto (copyable project id, "Sandbox" badge) and Sobre (version from `package.json`, README and docs links). No placeholder toggles.
