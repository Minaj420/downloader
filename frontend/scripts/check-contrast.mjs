#!/usr/bin/env node
// Contrast contract check (UI-A11Y-1). The contract itself is documented in
// docs/design/UI_DIRECTION.md §2 "Contrast Contract".
//
// Enforces the allowed meaningful foreground/background/border token pairs
// against the AUTHORITATIVE token values in frontend/src/styles.css:
//   - light theme: the first `:root { ... }` block
//   - dark theme:  `:root { ... }` inside `@media (prefers-color-scheme: dark)`
//     (inherits every light token, then applies its overrides)
//
// Deliberately NOT a general CSS parser: a narrow, known-block regex read of
// `#RRGGBB` custom properties only. No dependencies, deterministic output.
// `color-mix()` and other derived colours are out of scope by design.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const stylesPath = join(scriptDir, "..", "src", "styles.css");

// ---------------------------------------------------------------------------
// Contract: allowed meaningful token pairs (light AND dark are both checked).
// Keep this list in sync with the Contrast Contract table in
// docs/design/UI_DIRECTION.md §2. Never copy hex VALUES here; tokens are
// resolved from styles.css so the check enforces the authoritative CSS.
// ---------------------------------------------------------------------------

const CATEGORY_THRESHOLDS = {
  "normal-text": 4.5, // < 24px regular / < 18.66px bold
  "large-text": 3, // >= 24px regular or >= 18.66px bold
  "ui-boundary": 3, // meaningful control boundary / non-text state indicator
};

const CONTRACT = [
  // body text on the page surface
  { id: "text/surface", fg: "--color-text", bg: "--color-surface", category: "normal-text", threshold: 4.5 },
  // text on raised panels, inputs, selects
  { id: "text/surface-raised", fg: "--color-text", bg: "--color-surface-raised", category: "normal-text", threshold: 4.5 },
  // text on tinted fills: default/secondary buttons, form header strip
  { id: "text/surface-tint", fg: "--color-text", bg: "--color-surface-tint", category: "normal-text", threshold: 4.5 },
  // muted text directly on the page surface (muted notes, empty-state text)
  { id: "text-muted/surface", fg: "--color-text-muted", bg: "--color-surface", category: "normal-text", threshold: 4.5 },
  // muted text inside raised panels (activity timestamps, input placeholders)
  { id: "text-muted/surface-raised", fg: "--color-text-muted", bg: "--color-surface-raised", category: "normal-text", threshold: 4.5 },
  // header text and backend status label on the ink shell
  { id: "header-text/ink-950", fg: "--color-header-text", bg: "--color-ink-950", category: "normal-text", threshold: 4.5 },
  // muted nav links on the ink shell
  { id: "header-text-muted/ink-950", fg: "--color-header-text-muted", bg: "--color-ink-950", category: "normal-text", threshold: 4.5 },
  // ink text on accent fill (primary button, numbered step plate)
  { id: "ink-950/accent", fg: "--color-ink-950", bg: "--color-accent", category: "normal-text", threshold: 4.5 },
  // accent text on ink fill (primary button hover inversion)
  { id: "accent/ink-950", fg: "--color-accent", bg: "--color-ink-950", category: "normal-text", threshold: 4.5 },
  // --color-on-status text on semantic status surfaces (badges)
  { id: "on-status/info", fg: "--color-on-status", bg: "--color-info", category: "normal-text", threshold: 4.5 },
  { id: "on-status/success", fg: "--color-on-status", bg: "--color-success", category: "normal-text", threshold: 4.5 },
  { id: "on-status/danger", fg: "--color-on-status", bg: "--color-danger", category: "normal-text", threshold: 4.5 },
  { id: "on-status/warning", fg: "--color-on-status", bg: "--color-warning", category: "normal-text", threshold: 4.5 },
  { id: "on-status/status-neutral", fg: "--color-on-status", bg: "--color-status-neutral", category: "normal-text", threshold: 4.5 },
  // danger text/border on the raised panels that host job errors and the
  // transparent Cancel button (real usage; not the page surface)
  { id: "danger-text/surface-raised", fg: "--color-danger-text", bg: "--color-surface-raised", category: "normal-text", threshold: 4.5 },
  // meaningful control boundary: input/select borders, quiet button
  // boundary and progress bar outer boundary on raised surfaces
  { id: "border-strong/surface-raised", fg: "--color-border-strong", bg: "--color-surface-raised", category: "ui-boundary", threshold: 3 },
  // default/secondary button boundary (strong border against tint fill)
  { id: "border-strong/surface-tint", fg: "--color-border-strong", bg: "--color-surface-tint", category: "ui-boundary", threshold: 3 },
  // non-text accent indicator (focus outline, native radio accent, primary
  // button outer boundary) against the meaningful surfaces it appears on
  { id: "accent-ui/surface", fg: "--color-accent-ui", bg: "--color-surface", category: "ui-boundary", threshold: 3 },
  { id: "accent-ui/surface-raised", fg: "--color-accent-ui", bg: "--color-surface-raised", category: "ui-boundary", threshold: 3 },
  { id: "accent-ui/surface-tint", fg: "--color-accent-ui", bg: "--color-surface-tint", category: "ui-boundary", threshold: 3 },
  { id: "accent-ui/ink-950", fg: "--color-accent-ui", bg: "--color-ink-950", category: "ui-boundary", threshold: 3 },
  // progress fill vs track: meaningful state indicator, direct hex tokens
  { id: "accent-ui/progress-track", fg: "--color-accent-ui", bg: "--color-progress-track", category: "ui-boundary", threshold: 3 },
];

// ---------------------------------------------------------------------------
// Narrow known-block extraction from styles.css (no CSS parser).
// ---------------------------------------------------------------------------

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function parseCustomProperties(block) {
  const props = new Map();
  const re = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
  let match;
  while ((match = re.exec(block)) !== null) {
    props.set(match[1], match[2].trim());
  }
  return props;
}

function readThemes() {
  const css = stripComments(readFileSync(stylesPath, "utf8"));

  // Light theme: the first `:root` block in the file.
  const lightBlock = css.match(/:root\s*\{([^{}]*)\}/);
  if (!lightBlock) {
    throw new Error("light `:root` block not found in styles.css");
  }

  // Dark theme: `:root` block inside the dark prefers-color-scheme media query.
  // `\n\}` matches the media query's own closing brace at column 0.
  const darkMedia = css.match(
    /@media \(prefers-color-scheme: dark\)\s*\{([\s\S]*?)\n\}/,
  );
  if (!darkMedia) {
    throw new Error(
      "`@media (prefers-color-scheme: dark)` block not found in styles.css",
    );
  }
  const darkBlock = darkMedia[1].match(/:root\s*\{([^{}]*)\}/);
  if (!darkBlock) {
    throw new Error("dark `:root` block not found in styles.css");
  }

  const light = parseCustomProperties(lightBlock[1]);
  const darkOverrides = parseCustomProperties(darkBlock[1]);
  // Dark inherits every light token, then applies its overrides.
  const dark = new Map(light);
  for (const [token, value] of darkOverrides) dark.set(token, value);

  return { light, dark };
}

// ---------------------------------------------------------------------------
// WCAG 2.x relative luminance + contrast ratio.
// ---------------------------------------------------------------------------

function channel(c8) {
  const c = c8 / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(hex) {
  return (
    0.2126 * channel(parseInt(hex.slice(1, 3), 16)) +
    0.7152 * channel(parseInt(hex.slice(3, 5), 16)) +
    0.0722 * channel(parseInt(hex.slice(5, 7), 16))
  );
}

function contrastRatio(hexA, hexB) {
  const la = relativeLuminance(hexA);
  const lb = relativeLuminance(hexB);
  const [lighter, darker] = la >= lb ? [la, lb] : [lb, la];
  return (lighter + 0.05) / (darker + 0.05);
}

// ---------------------------------------------------------------------------
// Check runner.
// ---------------------------------------------------------------------------

function resolveToken(theme, themeName, token) {
  const value = theme.get(token);
  if (value === undefined) {
    throw new Error(
      `token ${token} is not defined in the ${themeName} theme (frontend/src/styles.css)`,
    );
  }
  if (!/^#[0-9a-fA-F]{6}$/.test(value)) {
    throw new Error(
      `token ${token} has unsupported non-hex value "${value}" in the ${themeName} theme; ` +
        "the contrast contract only supports #RRGGBB tokens",
    );
  }
  return value;
}

function main() {
  for (const pair of CONTRACT) {
    const expected = CATEGORY_THRESHOLDS[pair.category];
    if (expected === undefined) {
      throw new Error(`pair ${pair.id}: unknown category "${pair.category}"`);
    }
    if (pair.threshold !== expected) {
      throw new Error(
        `pair ${pair.id}: threshold ${pair.threshold} does not match ` +
          `category "${pair.category}" (expected ${expected})`,
      );
    }
  }

  const { light, dark } = readThemes();
  const themes = [
    ["light", light],
    ["dark", dark],
  ];

  let pass = 0;
  const failures = [];

  for (const pair of CONTRACT) {
    for (const [themeName, theme] of themes) {
      const fgHex = resolveToken(theme, themeName, pair.fg);
      const bgHex = resolveToken(theme, themeName, pair.bg);
      const ratio = contrastRatio(fgHex, bgHex);
      const ok = ratio >= pair.threshold;
      if (ok) pass += 1;
      else {
        failures.push(
          `${pair.id} (${pair.category}, ${themeName}): ` +
            `${pair.fg} ${fgHex} on ${pair.bg} ${bgHex} = ${ratio.toFixed(2)}:1, ` +
            `required >= ${pair.threshold}:1`,
        );
      }
      console.log(
        `${ok ? "PASS" : "FAIL"} | ${pair.id} | ${pair.category} | ${themeName} | ` +
          `${pair.fg} ${fgHex} on ${pair.bg} ${bgHex} | ` +
          `${ratio.toFixed(2)}:1 | required >= ${pair.threshold}:1`,
      );
    }
  }

  if (failures.length > 0) {
    console.error(`\n${failures.length} contrast contract check(s) FAILED:`);
    for (const failure of failures) console.error(`  - ${failure}`);
    console.error(
      "Fix the token values in frontend/src/styles.css. If a UI task introduced a " +
        "new meaningful pair, add it to the Contrast Contract table in " +
        "docs/design/UI_DIRECTION.md §2 and to CONTRACT in this script.",
    );
    process.exit(1);
  }

  console.log(
    `\nOK: ${CONTRACT.length} contract pairs x ${themes.length} themes = ` +
      `${pass} checks passed (thresholds: normal-text >= 4.5:1, ` +
      `large-text >= 3:1, ui-boundary >= 3:1).`,
  );
}

try {
  main();
} catch (err) {
  console.error(`contrast check error: ${err.message}`);
  process.exit(1);
}
