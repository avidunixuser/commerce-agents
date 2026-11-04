import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../api.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText;
const { AgentApi, apiRoot, readEventStream } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

function stream(parts) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part));
      controller.close();
    },
  });
}

test("production root is always same origin, demo remains configurable", () => {
  const nodeEnv = process.env.NODE_ENV;
  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  try {
    process.env.NODE_ENV = "production";
    process.env.NEXT_PUBLIC_API_URL = "http://insecure.example";
    assert.equal(apiRoot("http://localhost:8000"), "");
    process.env.NODE_ENV = "development";
    assert.equal(apiRoot("http://localhost:8000"), "http://insecure.example");
    delete process.env.NEXT_PUBLIC_API_URL;
    assert.equal(apiRoot("http://localhost:8000"), "http://localhost:8000");
  } finally {
    if (nodeEnv === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = nodeEnv;
    if (apiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL; else process.env.NEXT_PUBLIC_API_URL = apiUrl;
  }
});

test("SSE parser handles split UTF8 frames, CRLF, comments, IDs and multiline data", async () => {
  const frames = [];
  for await (const frame of readEventStream(stream([
    ": heartbeat\r\nid: 17\r\nevent: text_delta\r\nda",
    "ta: {\"text\":\r\ndata: \"hello\"}\r\n\r\n",
    "id: 18\nevent: turn_complete\ndata: {}\n\n",
  ]))) frames.push(frame);
  assert.deepEqual(frames, [
    { id: "17", event: { type: "text_delta", data: { text: "hello" } } },
    { id: "18", event: { type: "turn_complete", data: {} } },
  ]);
});

test("malformed SSE frames surface errors instead of silently losing events", async () => {
  await assert.rejects(async () => {
    for await (const ignored of readEventStream(stream(["event: text_delta\ndata: invalid\n\n"]))) {
      void ignored;
    }
  }, SyntaxError);
});

test("SSE frame size is bounded across multiple data lines", async () => {
  await assert.rejects(async () => {
    for await (const ignored of readEventStream(stream([
      `event: text_delta\ndata: ${"x".repeat(600000)}\ndata: ${"x".repeat(600000)}\n\n`,
    ]))) void ignored;
  }, /size limit/);
});

test("accepted mutations wait for the durable result and deduplicate concurrent submissions", async () => {
  const original = globalThis.fetch;
  const calls = [];
  let release;
  let markPolled;
  const status = new Promise((resolve) => { release = resolve; });
  const polled = new Promise((resolve) => { markPolled = resolve; });
  try {
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      if (init.method === "POST") return Response.json({ status: "accepted", turn_id: "turn1" }, { status: 202 });
      markPolled();
      return status;
    };
    const api = new AgentApi("", "/api");
    api.session = "session1";
    let completed = false;
    const request = api.post("/cart/add", { product_id: "p1" });
    void request.then(() => { completed = true; });
    await polled;
    const sameRequest = api.post("/cart/add", { product_id: "p1" });
    assert.equal(completed, false);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].url, "/api/turns/turn1");
    assert.equal(calls[1].init.headers["X-Session-Id"], "session1");
    assert.match(calls[0].init.headers["Idempotency-Key"], /^[a-f\d-]{36}$/);
    release(Response.json({ status: "complete", result: { cart: { count: 1 } } }));
    assert.deepEqual(await request, { cart: { count: 1 } });
    assert.deepEqual(await sameRequest, { cart: { count: 1 } });
    assert.equal(calls.filter((call) => call.init.method === "POST").length, 1);
  } finally { globalThis.fetch = original; }
});

test("uncertain mutation retries only read status and never repeat the action", async () => {
  const original = globalThis.fetch;
  const calls = [];
  let phase = "uncertain";
  try {
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      if (init.method === "POST") return Response.json({ status: "accepted", turn_id: "turn2" }, { status: 202 });
      return Response.json({ status: phase, result: { change: { status: "applied" } } });
    };
    const api = new AgentApi("", "/api/merchant");
    assert.equal(await api.post("/changes/change1/apply"), null);
    assert.equal(await api.post("/changes/change1/apply"), null);
    phase = "complete";
    assert.deepEqual(await api.post("/changes/change1/apply"), { change: { status: "applied" } });
    assert.equal(calls.filter((call) => call.init.method === "POST").length, 1);
    assert.equal(calls.filter((call) => call.url === "/api/merchant/turns/turn2").length, 3);
  } finally { globalThis.fetch = original; }
});

test("accepted actions without a durable ID cannot be blindly resubmitted", async () => {
  const original = globalThis.fetch;
  let count = 0;
  try {
    globalThis.fetch = async () => {
      count++;
      return Response.json({ status: "accepted" }, { status: 202 });
    };
    const api = new AgentApi("", "/api");
    assert.equal(await api.post("/cart/add", { product_id: "p1" }), null);
    assert.equal(await api.post("/cart/add", { product_id: "p1" }), null);
    assert.equal(count, 1);
  } finally { globalThis.fetch = original; }
});

test("stream reconnect uses durable cursor and never replays POST", async () => {
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      if (init.method === "POST") return new Response(stream([
        "id: 1\nevent: text_delta\ndata: {\"text\":\"one\"}\n\n",
      ]), { headers: { "X-Turn-Id": "turn1" } });
      return new Response(stream([
        "id: 1\nevent: text_delta\ndata: {\"text\":\"one\"}\n\n",
        "id: 2\nevent: text_delta\ndata: {\"text\":\"two\"}\n\n",
        "id: 3\nevent: turn_complete\ndata: {}\n\n",
      ]));
    };
    const api = new AgentApi("", "/api");
    api.session = "session1";
    api.csrf = "csrf1";
    const events = [];
    for await (const event of api.chatStream("hello")) events.push(event);
    assert.deepEqual(events.map((event) => event.data.text).filter(Boolean), ["one", "two"]);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].init.headers["X-CSRF-Token"], "csrf1");
    assert.match(calls[0].init.headers["Idempotency-Key"], /^[a-f\d-]{36}$/);
    assert.equal(calls[1].url, "/api/turns/turn1/events");
    assert.equal(calls[1].init.headers["Last-Event-ID"], "1");
    assert.equal(calls[1].init.method, undefined);
  } finally { globalThis.fetch = original; }
});

test("cancellation aborts transport and invokes durable cancellation", async () => {
  const original = globalThis.fetch;
  const calls = [];
  let api;
  try {
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      if (url.endsWith("/cancel")) return Response.json({ ok: true });
      return new Response(stream(["id: 1\nevent: text_delta\ndata: {\"text\":\"one\"}\n\n"]), {
        headers: { "X-Turn-Id": "turn1" },
      });
    };
    api = new AgentApi("", "/api");
    const generator = api.chatStream("hello");
    await generator.next();
    await api.cancelChat();
    assert.equal(calls[0].init.signal.aborted, true);
    assert.equal(calls[1].url, "/api/turns/turn1/cancel");
    await generator.return();
  } finally { globalThis.fetch = original; }
});
