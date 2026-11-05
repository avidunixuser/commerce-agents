import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const shared = fileURLToPath(new URL("../", import.meta.url));
const examples = resolve(shared, "..");
const classes = {
  retail: "font-retail",
  travel: "font-travel",
  telecom: "font-mobile",
  entertainment: "font-tickets",
};

for (const [vertical, className] of Object.entries(classes)) {
  test(`${vertical} portals use packaged fonts and local font assets`, async () => {
    const styles = await readFile(resolve(shared, `${vertical}-fonts.css`), "utf8");
    assert.ok(styles.includes(`.${className} {`));
    assert.doesNotMatch(styles, /https?:|fonts\.googleapis\.com|fonts\.gstatic\.com/);
    const imports = [...styles.matchAll(/@import\s+"([^"]+)";/g)].map((match) => match[1]);
    assert.ok(imports.length > 0);
    for (const specifier of imports) {
      assert.match(specifier, /^@fontsource(?:-variable)?\//);
      const path = require.resolve(specifier);
      const css = await readFile(path, "utf8");
      assert.doesNotMatch(css, /https?:|@import/);
      assert.ok(css.includes("font-display: swap;"));
      const assets = [...css.matchAll(/url\(([^)]+)\)/g)].map((match) => match[1]);
      assert.ok(assets.length > 0);
      for (const asset of assets) {
        assert.match(asset, /^\.\/files\/[^/]+\.woff2?$/);
        assert.ok((await stat(resolve(dirname(path), asset))).size > 0);
      }
    }
    for (const portal of ["storefront-web", "merchant-web"]) {
      const layout = await readFile(resolve(examples, vertical, portal, "app", "layout.tsx"), "utf8");
      assert.ok(layout.includes(`import "web-shared/${vertical}-fonts.css";`));
      assert.ok(layout.includes(`className="${className}"`));
      assert.doesNotMatch(layout, /next\/font\/google|fonts\.googleapis\.com|fonts\.gstatic\.com/);
    }
  });
}

test("retail keeps Instrument Sans variable family and full weight range", async () => {
  const styles = await readFile(resolve(shared, "retail-fonts.css"), "utf8");
  assert.ok(styles.includes('--font-body: "Instrument Sans Variable", sans-serif;'));
  const css = await readFile(require.resolve("@fontsource-variable/instrument-sans"), "utf8");
  assert.ok(css.includes("font-family: 'Instrument Sans Variable';"));
  assert.ok(css.includes("font-weight: 400 700;"));
});

test("travel keeps optical-size variable faces in normal and italic", async () => {
  const styles = await readFile(resolve(shared, "travel-fonts.css"), "utf8");
  for (const [file, style] of [["opsz.css", "normal"], ["opsz-italic.css", "italic"]]) {
    const specifier = `@fontsource-variable/fraunces/${file}`;
    assert.ok(styles.includes(`"${specifier}"`));
    const css = await readFile(require.resolve(specifier), "utf8");
    assert.ok(css.includes(`font-style: ${style};`));
    assert.ok(css.includes("font-weight: 100 900;"));
  }
});

test("telecom keeps all seven selected Barlow weights and variable mono", async () => {
  const styles = await readFile(resolve(shared, "telecom-fonts.css"), "utf8");
  for (const weight of [100, 300, 400, 500, 600, 700, 800]) {
    const specifier = `@fontsource/barlow/latin-${weight}.css`;
    assert.ok(styles.includes(`"${specifier}"`));
    assert.ok((await readFile(require.resolve(specifier), "utf8")).includes(`font-weight: ${weight};`));
  }
  const mono = await readFile(require.resolve("@fontsource-variable/spline-sans-mono"), "utf8");
  assert.ok(mono.includes("font-weight: 300 700;"));
});

test("runtime image preserves every packaged font license", async () => {
  const dockerfile = await readFile(resolve(examples, "..", "Dockerfile.web"), "utf8");
  const packages = new Set();
  for (const vertical of Object.keys(classes)) {
    const styles = await readFile(resolve(shared, `${vertical}-fonts.css`), "utf8");
    for (const match of styles.matchAll(/@import\s+"([^"]+)";/g)) {
      packages.add(match[1].split("/").slice(0, 2).join("/"));
    }
  }
  for (const name of packages) {
    assert.ok(dockerfile.includes(`node_modules/${name}/LICENSE /output/licenses/`));
    const license = await readFile(resolve(examples, "node_modules", name, "LICENSE"), "utf8");
    assert.match(license, /SIL OPEN FONT LICENSE/i);
  }
});
