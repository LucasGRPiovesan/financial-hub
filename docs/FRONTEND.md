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
grows from 16 px on phones to 32 px on wide screens. Tables use a 40 px header row and `py-2.5` cells with hairline
separators (no zebra); headers are sticky inside scrolling regions. Radii are tight
(3 / 5 / 7 px) and only floating layers (dialogs, menus) use 11 px. Shadows are a hairline plus a low ambient; only
overlays get a real lift.

### Motion

Sparing, and always an answer to something the operator did: dialogs and menus fade/pop in (140-160 ms), the nav and
detail drawers slide (200 ms), toasts rise (220 ms). Nothing animates on its own. `prefers-reduced-motion: reduce`
collapses every animation and transition to ~0.

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
- **API keys.** The plaintext key is kept out of every query/mutation cache (`features/console/api.ts`).
- **Code splitting.** Screens are `React.lazy` routes (via `lazyRouteComponent`); the initial chunk is ~466 kB (was
  ~539 kB) with login and the shell eager.
- **Dev proxy.** `vite.config.ts` proxies `/v1`, `/health` and the legacy `^/api/` prefix. The `/api` rule is anchored
  with a slash so the SPA route `/api-keys` is not swallowed on a hard reload.

## Testing

`scripts/web-test.sh` (vitest + Testing Library + axe). Prefer role/label queries; the e2e suite relies on the same
accessible names (`Continue as <name>`, `Project: <name>`, `Operator menu: <name>`, `Theme: <mode>`, `Sign out`,
nav labels, dialog field labels). Rename one only together with the e2e suite.

### Polish notes (W9.10)

- Page headers carry no "Implemented" badge; only non-default scopes (Sandbox) and planned areas are marked
  (a dotted "Soon" tag in the sidebar, generated content so link names are unchanged).
- A hard load shows `BootScreen` (shell silhouette, motion-safe pulse) instead of a blank page.
- Recent activity text comes from `features/console/audit-activity.ts` (pure, table-tested), using only audit fields.
