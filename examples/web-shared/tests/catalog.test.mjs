import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../catalog.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText.replace(
  /import \{ useEffect, useState \} from "react";/,
  "const useEffect=(...a)=>globalThis.__catalogHooks.effect(...a); const useState=(...a)=>globalThis.__catalogHooks.state(...a);",
).replace(
  /import \{ useStoreFrame \} from "\.\/storefront\/frame";/,
  "const useStoreFrame=()=>globalThis.__catalogFrame;",
);
const { useCatalogIndex } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

function mounted(session) {
  let state;
  let initialized = false;
  let deps;
  let cleanup;
  let effect;
  return {
    session,
    render(load, override) {
      globalThis.__catalogFrame = { catalogSession: this.session };
      globalThis.__catalogHooks = {
        state(initial) {
          if (!initialized) { state = initial; initialized = true; }
          return [state, (value) => { state = value; }];
        },
        effect(run, next) {
          if (!deps || next.some((value, i) => value !== deps[i])) {
            cleanup?.();
            deps = next;
            effect = run;
          }
        },
      };
      const value = useCatalogIndex(load, override);
      if (effect) { cleanup = effect(); effect = undefined; }
      return value;
    },
    unmount() { cleanup?.(); },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("catalog waits for the committed session and shares one read across mounted consumers", async () => {
  let calls = 0;
  const load = async () => { calls++; return [{ product_id: "p1" }]; };
  const first = mounted(null);
  const second = mounted(null);
  assert.deepEqual(first.render(load), {});
  assert.deepEqual(second.render(load), {});
  await settle();
  assert.equal(calls, 0);
  first.session = second.session = "session1";
  first.render(load);
  second.render(load);
  await settle();
  assert.equal(calls, 1);
  assert.deepEqual(first.render(load), { p1: { product_id: "p1" } });
  assert.deepEqual(second.render(load), { p1: { product_id: "p1" } });
  first.unmount();
  second.unmount();
});

test("session changes immediately hide old catalog data and ignore an old in-flight read", async () => {
  let release;
  let calls = 0;
  const load = () => ++calls === 1
    ? new Promise((resolve) => { release = resolve; })
    : Promise.resolve([{ product_id: "new" }]);
  const view = mounted("old-session");
  view.render(load);
  view.session = "new-session";
  assert.deepEqual(view.render(load), {});
  await settle();
  release([{ product_id: "old" }]);
  await settle();
  assert.deepEqual(view.render(load), { new: { product_id: "new" } });
  view.session = null;
  assert.deepEqual(view.render(load), {});
  view.unmount();
});

test("failed catalog responses are not permanently cached as an empty successful catalog", async () => {
  let calls = 0;
  const load = async () => ++calls === 1 ? null : [{ product_id: "retry" }];
  const first = mounted("same-session");
  first.render(load);
  await settle();
  assert.deepEqual(first.render(load), {});
  first.unmount();
  const retry = mounted("same-session");
  retry.render(load);
  await settle();
  assert.equal(calls, 2);
  assert.deepEqual(retry.render(load), { retry: { product_id: "retry" } });
  retry.unmount();
});

test("an explicit session gates a catalog hook outside the storefront frame", async () => {
  let calls = 0;
  const load = async () => { calls++; return []; };
  const view = mounted(undefined);
  view.render(load, null);
  await settle();
  assert.equal(calls, 0);
  view.render(load, "telecom-session");
  await settle();
  assert.equal(calls, 1);
  view.unmount();
});

test("rejected catalog reads report the error and can be retried after remount", async () => {
  let calls = 0;
  const load = async () => {
    if (++calls === 1) throw new Error("Catalog unavailable");
    return [{ product_id: "recovered" }];
  };
  const previousWindow = globalThis.window;
  const errors = [];
  globalThis.window = { dispatchEvent: (event) => errors.push(event.detail) };
  const first = mounted("retry-session");
  const retry = mounted("retry-session");
  try {
    first.render(load);
    await settle();
    assert.deepEqual(errors, ["Catalog unavailable"]);
    first.unmount();
    retry.render(load);
    await settle();
    assert.equal(calls, 2);
    assert.deepEqual(retry.render(load), { recovered: { product_id: "recovered" } });
  } finally {
    first.unmount();
    retry.unmount();
    globalThis.window = previousWindow;
  }
});

test("unbound demo showcases keep their catalog reads", async () => {
  let calls = 0;
  const load = async () => { calls++; return [{ product_id: "demo" }]; };
  const view = mounted(undefined);
  view.render(load);
  await settle();
  assert.equal(calls, 1);
  assert.deepEqual(view.render(load), { demo: { product_id: "demo" } });
  view.unmount();
});
