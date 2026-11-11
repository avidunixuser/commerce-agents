import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const root = new URL("../", import.meta.url);
const source = await readFile(new URL("ZavaLogo.tsx", root), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ES2022,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText
  .replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve("react/jsx-runtime")))
  .replace(/import icon from ".\/assets\/zava-icon.png";/, 'const icon={src:"/icon.png",width:336,height:330};')
  .replace(/import logo from ".\/assets\/zava-logo.png";/, 'const logo={src:"/logo.png",width:1196,height:330};');
const { ZavaLogo } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

test("full and compact logos render the provided assets with accessible names and stable dimensions", () => {
  const full = renderToStaticMarkup(createElement(ZavaLogo));
  const compact = renderToStaticMarkup(createElement(ZavaLogo, { compact: true }));
  assert.match(full, /src="\/logo.png"/);
  assert.match(full, /width="1196" height="330"/);
  assert.match(compact, /src="\/icon.png"/);
  assert.match(compact, /width="336" height="330"/);
  for (const html of [full, compact]) {
    assert.match(html, /alt="Zava Agentic AI"/);
    assert.match(html, /h-auto/);
  }
});

for (const vertical of ["retail", "travel", "telecom", "entertainment"]) {
  for (const app of ["storefront-web", "merchant-web"]) {
    test(`${vertical} ${app} uses the shared artwork, Zava metadata and matching app icon`, async () => {
      const base = new URL(`../${vertical}/${app}/`, root);
      const page = await readFile(new URL("app/page.tsx", base), "utf8");
      const layout = await readFile(new URL("app/layout.tsx", base), "utf8");
      const manifest = JSON.parse(await readFile(new URL("package.json", base), "utf8"));
      assert.match(page, /<ZavaLogo/);
      if (app === "merchant-web") {
        assert.match(page, /mark: <ZavaLogo compact \/>/);
        assert.match(page, /logo: <ZavaLogo \/>/);
      }
      assert.match(layout, /title: "Zava/);
      assert.equal(manifest.name, `zava-${vertical}-${app}`);
      const icon = await readFile(new URL("app/icon.png", base));
      assert.deepEqual(icon, await readFile(new URL("assets/zava-icon.png", root)));
    });
  }
}
