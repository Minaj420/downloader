# Downloader — UI Design Direction

**Status:** Authoritative implementation guide for frontend visual work.

**Visual reference:** `docs/design/references/downloader-main-reference.png`

**Style-source rule:** The reference image is a **style source, not a product
specification**. Visual language (colour, typography, panel grammar, density)
is derived from it. Product behaviour, screen structure, actions, and scope
are derived only from `PRODUCT_SPEC.md` and `ARCHITECTURE.md`. Where the two
conflict, the specs win.

---

## 1. Visual character

Modern utility console with late-90s / early-2000s industrial download-manager
character, cleaned up for modern usability:

- Dark navy application shell, very light cool-blue working surfaces,
  controlled orange accent.
- Tight, operational, information-first layout — not a marketing page.
- Sharp, angular feel: small radii, hairline borders, flat fills.
- Selected angular/clip-corner motifs on headers and accent blocks only.
- Decorative technical motifs (hairlines, dot matrix, halftone/photo texture)
  confined to the shell, empty states, and branding spots.

---

## 2. Colour and tokens

Implement as CSS custom properties in `frontend/src/styles.css`; no CSS
framework or UI library. Baseline values (final values pass contrast checks,
WCAG AA for body text):

```css
:root {
  /* shell + surfaces */
  --color-ink-950: #0d2032;
  --color-ink-800: #17344d;
  --color-surface: #f7fbfe;
  --color-surface-raised: #ffffff;
  --color-surface-tint: #e8f3fa;
  --color-border: #9bb8cc;
  --color-border-strong: #55758d;

  /* semantic */
  --color-accent: #f66a0a;
  --color-accent-hover: #d95300;
  --color-info: #1769aa;
  --color-success: #237a57;
  --color-warning: #9a5b00;
  --color-danger: #b42318;

  /* non-text accent: focus outline, radio accent, progress fill/track */
  --color-accent-ui: #d95300;
  --color-progress-track: #17344d;

  /* derived text tokens (components never hard-code colours) */
  --color-text: #1c2530;
  --color-text-muted: #55758d;
  --color-on-status: #ffffff;
  --color-header-text: #f2f8fc;
  --color-header-text-muted: #a9c1d3;
  --color-status-neutral: #55758d;
  --color-danger-text: #b42318;

  /* geometry + type */
  --font-body: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-display: system-ui, -apple-system, "Arial Narrow", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --space-1: 0.25rem;  --space-2: 0.5rem;  --space-3: 0.75rem;
  --space-4: 1rem;     --space-6: 1.5rem;  --space-8: 2rem;
  --radius-control: 4px;
  --radius-panel: 6px;
  --border-default: 1px solid var(--color-border);
  --focus-ring: 0 0 0 3px rgba(246, 106, 10, 0.35);
}

/* Dark mapping: token swap only; component CSS stays unchanged. */
@media (prefers-color-scheme: dark) {
  :root {
    --color-surface: #0f1a24;
    --color-surface-raised: #16232f;
    --color-surface-tint: #1b2c3a;
    --color-border: #34506a;
    --color-border-strong: #7492ab;
    --color-text: #e6eef5;
    --color-text-muted: #9fb6c7;
    --color-header-text-muted: #8fabbf;
    --color-status-neutral: #4a6379;
    --color-danger-text: #f2705e;
    --color-accent-ui: #f66a0a;
    --color-progress-track: #24405c;
  }
}
```

Rules:

- Components consume tokens only; no hard-coded colours in component CSS.
- Orange = accent only: active navigation, primary action, progress fill,
  emphasis markers. Orange is never an error/destructive colour. Non-text
  accent usages that must meet ≥3:1 (focus outline, native radio accent,
  progress fill/track) use the dedicated contrast-safe tokens
  `--color-accent-ui` / `--color-progress-track`, not `--color-accent`.
- Danger is a distinct red system, used for destructive actions and errors.
- Tokens are defined for the light surface first. The dark theme is a
  **token-only swap** inside `@media (prefers-color-scheme: dark)` that
  inherits every light token and overrides only surfaces, borders, and text
  tokens; component CSS never changes between themes.

### Contrast Contract (UI-A11Y-1)

The allowed meaningful text/background/border token pairs for the light and
dark themes are fixed by the table below. Thresholds (WCAG 2.x contrast):

- **normal text:** ≥ 4.5:1
- **large text:** ≥ 3:1 (≈ 24px regular or 18.66px bold and above)
- **meaningful UI boundary / non-text state indicator:** ≥ 3:1

Exemption: decorative hairlines and dividers drawn with `--color-border`
(`--border-default` panel and panel-header borders) are panel furniture.
They are not meaningful control boundaries and do not alone indicate state,
so they are outside the 3:1 requirement. Meaningful control boundaries
(input/button borders, focus outline, radio accent) are checked. The
progress bar's separation from its raised panel is provided by a strong
outer border (`border-strong/surface-raised`), and the progress state
itself by the fill/track pair (`accent-ui/progress-track`); the progress
outer boundary is **not** covered by the hairline exemption. There is
currently no pair that relies on the large-text allowance; the
`large-text` category exists for future pairs.

Allowed pairs (both themes are checked; the dark theme inherits light
tokens and applies the §2 dark overrides):

| Pair id | Foreground | Background | Category | Threshold | Used by |
| --- | --- | --- | --- | --- | --- |
| `text/surface` | `--color-text` | `--color-surface` | normal-text | 4.5 | body text |
| `text/surface-raised` | `--color-text` | `--color-surface-raised` | normal-text | 4.5 | inputs, selects, panels |
| `text/surface-tint` | `--color-text` | `--color-surface-tint` | normal-text | 4.5 | default/secondary buttons, form header strip, collection preview items |
| `text-muted/surface` | `--color-text-muted` | `--color-surface` | normal-text | 4.5 | muted notes, empty-state text on the page surface |
| `text-muted/surface-raised` | `--color-text-muted` | `--color-surface-raised` | normal-text | 4.5 | activity timestamps, input placeholder text |
| `header-text/ink-950` | `--color-header-text` | `--color-ink-950` | normal-text | 4.5 | app header text, backend status label |
| `header-text-muted/ink-950` | `--color-header-text-muted` | `--color-ink-950` | normal-text | 4.5 | navigation links |
| `ink-950/accent` | `--color-ink-950` | `--color-accent` | normal-text | 4.5 | primary button, numbered step plate |
| `accent/ink-950` | `--color-accent` | `--color-ink-950` | normal-text | 4.5 | primary button hover inversion |
| `on-status/info` | `--color-on-status` | `--color-info` | normal-text | 4.5 | info status badges, platform badge |
| `on-status/success` | `--color-on-status` | `--color-success` | normal-text | 4.5 | Completed badge |
| `on-status/danger` | `--color-on-status` | `--color-danger` | normal-text | 4.5 | Failed badge |
| `on-status/warning` | `--color-on-status` | `--color-warning` | normal-text | 4.5 | Cancelled badge |
| `on-status/status-neutral` | `--color-on-status` | `--color-status-neutral` | normal-text | 4.5 | Queued badge |
| `danger-text/surface-raised` | `--color-danger-text` | `--color-surface-raised` | normal-text | 4.5 | job error text, danger button text/border (on raised panels) |
| `border-strong/surface-raised` | `--color-border-strong` | `--color-surface-raised` | ui-boundary | 3 | input/select borders, quiet button boundary, progress bar outer boundary |
| `border-strong/surface-tint` | `--color-border-strong` | `--color-surface-tint` | ui-boundary | 3 | default/secondary button boundary (strong border against tint fill) |
| `accent-ui/surface` | `--color-accent-ui` | `--color-surface` | ui-boundary | 3 | focus outline, radio accent on the page surface, primary Download button outer boundary |
| `accent-ui/surface-raised` | `--color-accent-ui` | `--color-surface-raised` | ui-boundary | 3 | focus outline on inputs/panels, radio accent on raised, primary Analyze button outer boundary |
| `accent-ui/surface-tint` | `--color-accent-ui` | `--color-surface-tint` | ui-boundary | 3 | focus outline on tinted buttons, radio accent on tinted lists |
| `accent-ui/ink-950` | `--color-accent-ui` | `--color-ink-950` | ui-boundary | 3 | focus outline on header navigation (ink shell) |
| `accent-ui/progress-track` | `--color-accent-ui` | `--color-progress-track` | ui-boundary | 3 | progress fill vs track (state indicator) |

`--color-text-muted` on `--color-surface-tint` is **not** an allowed pair
(measures ≈ 4.31:1 in light, below 4.5). It has no current usage — collection
preview position numbers were moved to `--color-text` — and muted text on
tinted fills must not be introduced.

**Acceptance rule for new/changed pairs:** any UI task that introduces or
changes a meaningful foreground/background/border token pair must add the
pair to the table above **and** to the `CONTRACT` list in
`frontend/scripts/check-contrast.mjs`, and the resulting light/dark ratios
must appear in the implementation/check output. A pair that is not in the
contract and not covered by a documented exemption is a defect.

**Coverage rule (bounded selector/state/context):** contrast coverage is
reasoned per real production occurrence, never by a global CSS
cross-product. For each meaningful occurrence, record: the selector, the
state (normal / hover / focus / disabled …), the visual role (text, control
boundary, focus indicator, radio accent, progress fill/track), the token,
the actual adjacent surface/parent, and the contract pair or named
exemption that covers it. Witness-edge rule: check the worst real context;
do not invent contexts that cannot occur in production.

Inclusion (each must map to a contract pair): visible normal text; required
control boundaries; focus outline and native radio accent; progress
fill/track; normal and hover states when both are real production contexts.

Named exemptions (no pair required): decorative panel/divider hairlines;
the redundant backend status dot (always paired with a text label);
decorative banner borders; the disabled opacity state; the focus box-shadow
halo when the outline carries the contrast; non-informative color-mix
overlays. The progress outer boundary is **not** exempt.

Current usage coverage:

| Real production context | Contract pair / exemption |
| --- | --- |
| Primary button normal+hover outer boundary on page surface (Download) and raised panel (Analyze) | `accent-ui/surface`, `accent-ui/surface-raised` |
| Default/secondary button boundary on tint fill | `border-strong/surface-tint` |
| Input/select and progress bar outer boundary on raised | `border-strong/surface-raised` |
| Focus outline, radio accent, progress fill/track | existing `accent-ui/*` pairs |
| Body/muted text, status badges, danger text, placeholder | existing normal-text contract rows |
| Decorative hairlines, status dot, banner borders, disabled state, focus halo, color-mix overlays | named exemptions |

**Enforcement:** `npm run check:contrast` in `frontend/` is the enforcement
tool for this contract. It reads the authoritative token values directly
from `frontend/src/styles.css` (light `:root` block and the dark
`@media (prefers-color-scheme: dark)` overrides; the dark theme inherits
light tokens) and fails on a missing token, a non-hex contract token, or
any below-threshold pair in either theme. It deliberately does not resolve
`color-mix()` or other derived colours; the contract is defined directly on
hex token pairs.

---

## 3. Typography

- **No external web-font dependency.** System fallback stacks only
  (`--font-body`, `--font-display`, `--font-mono` above).
- Display/headings: condensed-feel system stack, uppercase or strongly
  weighted section titles with letter-spacing; keep hierarchy from `h1`–`h3`
  semantics.
- Body: `--font-body`. **Desktop body text must stay in the 14–16px readable
  range.** Do not import the reference's tiny micro-label sizes.
- Numbers, timestamps, sizes, speeds: `--font-mono` with
  `font-variant-numeric: tabular-nums`.
- Status is never colour-only; every status colour is paired with a text
  label.

---

## 4. Panels, header, navigation

- **App shell:** full-width dark navy header bar; light content area with a
  generous but dense max width (wider than the current 60rem where the
  screen benefits, e.g. queue/library tables).
- **Header contents:** brand block, primary navigation, backend status
  (accessible text label stays — `Backend online` / `Backend offline`).
- **Primary navigation:** the four routes
  `New Download / Queue / Library / Settings` stay as router links
  (`react-router-dom` `NavLink`). Style them with the reference's angled-tab
  language; active route gets the orange accent plus a text/weight cue.
  Do not convert them to ARIA tabs — they are navigation, not in-page tabs.
- **Panels:** `--radius-panel`, hairline border, minimal shadow, and a
  distinct panel header strip (title + optional short subtitle).
- **Numbered section plates (`01`, `02`, `03`):** allowed only where the
  workflow is genuinely sequential within one screen (New Download flow).
  Never on unrelated panels.
- **In-page tabs** (e.g. video/audio format groups) may use tab styling but
  must follow real tab semantics when implemented as such.

---

## 5. Buttons, inputs, status, progress

Buttons (single visual family, small radius `--radius-control`):

- `primary` — orange fill, dark text/ink contrast, for the one main action
  per view (Analyze, Download).
- `secondary` — cool-blue tinted/outline fill for supporting actions
  (Use detected platform, Play-style affordances later).
- `quiet` — transparent/ghost for toggles like Activity disclosure.
- `danger` — red system, reserved for destructive flows (Cancel is
  conservative; Delete File is destructive).
- All buttons: visible `:focus-visible` ring (`--focus-ring`), disabled at
  ≥ `opacity: 0.55` with `cursor: not-allowed`, min touch target 44px on
  coarse pointers.
- Icon-only buttons must carry an accessible name; never reuse one ambiguous
  glyph (X / trash) for different actions (Cancel vs Remove vs Delete File).

Inputs:

- Light raised surface, 1px steel border, `--radius-control`, clear
  `:focus-visible`.
- **Form inputs keep a font size that avoids mobile focus zoom (16px on
  coarse pointers; never below 16px for `input`/`select` on mobile).**
- Labels are always visible; placeholder is never the only label.
- Select keeps native behaviour (Platform dropdown is a spec requirement).

Status:

- Status badges: small rounded rectangle, semantic colour + text
  (`Queued`, `Downloading`, `Completed`, `Failed`, `Cancelled`, …). Same
  vocabulary and colours app-wide.

Progress:

- Progress bar always paired with human-readable meta: percent, downloaded /
  total bytes, speed, ETA (as available), plus the status label.
- `role="progressbar"` with ARIA value attributes is preserved.
- Fills use accent orange; states use status colours, not the fill alone.

---

## 6. Responsive and accessibility

- Responsive from phone to desktop; phone is a first-class target
  (PRODUCT_SPEC §34). Desktop-dense rows (queue, library) collapse into
  stacked cards on mobile; no horizontal scrolling tables on small screens.
- Body text 14–16px on desktop; inputs 16px+ on mobile (no focus zoom).
- Touch targets ≥ 44px on coarse pointers.
- Colour contrast: body text meets WCAG AA against its surface; orange is
  checked against dark ink or white backgrounds before shipping. Meaningful
  token pairs follow the Contrast Contract in §2 (enforced by
  `npm run check:contrast`).
- Visible keyboard focus everywhere (`--focus-ring`); focus order follows
  DOM order; disclosure panels use `aria-expanded` / `aria-controls`.
- Live regions: errors `role="alert"`, background completions `role="status"`
  (existing conventions stay).
- Respect `prefers-reduced-motion`: progress transitions and any future
  animation degrade to instant state changes.
- Thumbnails/artwork are decorative (`alt=""`) unless they carry information
  not present in adjacent text.

---

## 7. Activity and Library patterns

Activity (PRODUCT_SPEC §18):

- Per-job collapsible disclosure, **collapsed by default** — never a global,
  always-open log sidebar like the reference.
- Structured `HH:MM:SS message` rows in mono font, tabular timestamps.
- Clean human-readable events only; no raw `yt-dlp` terminal output.
- Loading / empty / error states with retry, as implemented today.
- When reused across multiple job cards, panel IDs must be unique
  (current fixed `id="activity-list"` is single-use).

Library (PRODUCT_SPEC §20–21):

- Desktop: scannable list/table (`Name, Type, Size, Downloaded At, Format,
  Location, Path`); mobile: card per item carrying the same fields.
- Selecting an item reveals a detail panel (title, artwork, URL, platform,
  size, format, path, membership).
- Two clearly separated destructive actions:
  **Remove from Library** (record only, quiet/secondary styling) vs
  **Delete File** (red danger styling + explicit confirmation dialog).
  The interface must make the difference obvious at a glance.
- Deletion is ID-based through the API; no arbitrary filesystem paths.

---

## 8. Decorative elements — do not copy 1:1

Take the mood, not the artefacts. The following stay out of the product:

- Aggressive clip-corners on every panel (reserve for headers/accent).
- Low-contrast micro-text captions.
- Photo collages, model photography, slogan blocks ("Good media travels
  further"), fake ad banners.
- CPU / disk / network system monitors.
- `Convert`, `About` nav entries; `Pause All`, `Clear All`, drag handles,
  Play/Folder icon rows — no backend behaviour behind them.
- Fake fixed format lists (4K/1080p/720p…) — real options come from the
  backend analysis.
- Global always-on Activity stream; raw terminal-style output.
- Scanlines, glow, bevels, and any texture that hurts readability.

---

## 9. Behaviour rule

No screen, action, navigation entry, status, or flow may be invented because
it exists in the reference image. Product behaviour comes from
`PRODUCT_SPEC.md`; technical boundaries (backend-authoritative data, ID-based
APIs, normalized formats, job state machine) come from `ARCHITECTURE.md`.
The reference contributes **visual language only**.
