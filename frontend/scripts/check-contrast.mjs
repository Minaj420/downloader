#!/usr/bin/env node
// Small deterministic contrast guardrail for the token themes defined in
// frontend/src/styles.css. NOT an exhaustive or authoritative contract: the
// CHECKS list below is a short set of representative high-risk semantic
// combinations (body/muted/header text, primary action, status badges,
// danger text, control boundary, focus/accent indicator, progress fill).
// New or changed meaningful colour combinations are verified during
// implementation; they do not need to be added here.
//
// Reads the real token values directly from styles.css:
//   - light theme: the first `:root { ... }` block
//   - dark theme:  `:root { ... }` inside `@media (prefers-color-scheme: dark)`
//     (inherits every light token, then applies its overrides)
//
// Deliberately NOT a general CSS parser: a narrow, known-block regex read of
// `#RRGGBB` custom properties only. No dependencies, deterministic output.
// `color-mix()` and other derived colours are out of scope by design.
// Missing or non-hex tokens fail closed.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const stylesPath = join(scriptDir, "..", "src", "styles.css");

// Representative checks; both themes are checked for every entry.
// `min` is the WCAG threshold: 4.5 for normal text, 3 for meaningful UI
// boundaries / non-text state indicators.
const CHECKS = [
  { id: "body text on page surface", fg: "--color-text", bg: "--color-surface", min: 4.5 },
  { id: "muted text on raised panels", fg: "--color-text-muted", bg: "--color-surface-raised", min: 4.5 },
  { id: "header text on ink shell", fg: "--color-header-text", bg: "--color-ink-950", min: 4.5 },
  { id: "primary button text on accent", fg: "--color-ink-950", bg: "--color-accent", min: 4.5 },
  { id: "status badge text on success", fg: "--color-on-status", bg: "--color-success", min: 4.5 },
  { id: "status badge text on danger", fg: "--color-on-status", bg: "--color-danger", min: 4.5 },
  { id: "danger text on raised panels", fg: "--color-danger-text", bg: "--color-surface-raised", min: 4.5 },
  { id: "control boundary on raised panels", fg: "--color-border-strong", bg: "--color-surface-raised", min: 3 },
  { id: "focus/accent indicator on page surface", fg: "--color-accent-ui", bg: "--color-surface", min: 3 },
  { id: "progress fill vs track", fg: "--color-accent-ui", bg: "--color-progress-track", min: 3 },
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
        "the contrast check only supports #RRGGBB tokens",
    );
  }
  return value;
}

function main() {
  const { light, dark } = readThemes();
  const themes = [
    ["light", light],
    ["dark", dark],
  ];

  let pass = 0;
  const failures = [];

  for (const check of CHECKS) {
    for (const [themeName, theme] of themes) {
      const fgHex = resolveToken(theme, themeName, check.fg);
      const bgHex = resolveToken(theme, themeName, check.bg);
      const ratio = contrastRatio(fgHex, bgHex);
      const ok = ratio >= check.min;
      if (ok) pass += 1;
      else {
        failures.push(
          `${check.id} (${themeName}): ` +
            `${check.fg} ${fgHex} on ${check.bg} ${bgHex} = ${ratio.toFixed(2)}:1, ` +
            `required >= ${check.min}:1`,
        );
      }
      console.log(
        `${ok ? "PASS" : "FAIL"} | ${check.id} | ${themeName} | ` +
          `${check.fg} ${fgHex} on ${check.bg} ${bgHex} | ` +
          `${ratio.toFixed(2)}:1 | required >= ${check.min}:1`,
      );
    }
  }

  if (failures.length > 0) {
    console.error(`\nFAIL: ${failures.length} contrast check(s) failed:`);
    for (const failure of failures) console.error(`  - ${failure}`);
    console.error("Fix the token values in frontend/src/styles.css.");
    process.exit(1);
  }

  console.log(
    `\nPASS: ${CHECKS.length} checks x ${themes.length} themes = ` +
      `${pass} passed (thresholds: normal text >= 4.5:1, ` +
      `UI boundary / non-text indicator >= 3:1).`,
  );
}

try {
  main();
} catch (err) {
  console.error(`contrast check error: ${err.message}`);
  process.exit(1);
}
