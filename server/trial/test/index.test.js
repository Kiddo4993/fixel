// node --test server/trial/test  (no installs needed: Node 20+'s built-in runner)
import assert from "node:assert/strict";
import { test } from "node:test";

import { handle, validate, TrialCounter } from "../src/index.js";

const ID = "0123456789abcdef0123456789abcdef";

// Stands in for the COUNTER Durable Object namespace: one TrialCounter per name, with Map-backed storage.
function fakeCounters() {
  const instances = new Map();
  const make = () => {
    const data = new Map();
    let alarm = null;
    const storage = {
      data,
      list: async () => new Map(data),
      put: async (entries) => { for (const [k, v] of Object.entries(entries)) data.set(k, v); },
      getAlarm: async () => alarm,
      setAlarm: async (t) => { alarm = t; },
      deleteAll: async () => data.clear(),
    };
    return { storage, counter: new TrialCounter({ storage, blockConcurrencyWhile: (fn) => fn() }) };
  };
  return {
    instances,
    idFromName: (name) => name,
    get: (id) => {
      if (!instances.has(id)) instances.set(id, make());
      return { fetch: (url, init) => instances.get(id).counter.fetch(new Request(url, init)) };
    },
  };
}

function env(overrides = {}) {
  return { GEMINI_API_KEY: "secret-key", COUNTER: fakeCounters(), ...overrides };
}

const today = () => new Date().toISOString().slice(0, 10);

function ask(body = {}, ip = "1.2.3.4") {
  const payload = JSON.stringify({ install_id: ID, system: "sys", contents: [{ role: "user", parts: [{ text: "hi" }] }], ...body });
  return new Request("https://trial.example/v1/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
    body: payload,
  });
}

function gemini(statuses) {
  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const status = statuses.shift() ?? 200;
    return new Response(status === 200 ? 'data: {"candidates":[]}\n\n' : "{}", { status });
  };
  return { calls, upstream };
}

test("three questions a day, then trial_used", async () => {
  const e = env();
  for (const left of ["2", "1", "0"]) {
    const res = await handle(ask(), e, gemini([]).upstream);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("X-Fixel-Remaining"), left);
  }
  const res = await handle(ask(), e, gemini([]).upstream);
  assert.equal(res.status, 429);
  assert.equal((await res.json()).error, "trial_used");
});

test("the key goes only to Google, in a header", async () => {
  const g = gemini([]);
  await handle(ask(), env(), g.upstream);
  assert.equal(g.calls[0].init.headers["x-goog-api-key"], "secret-key");
  assert.ok(g.calls[0].url.startsWith("https://generativelanguage.googleapis.com/"));
  assert.ok(!g.calls[0].url.includes("secret-key"));
});

test("the client can't pick a paid model", async () => {
  const g = gemini([]);
  await handle(ask({ model: "gemini-3.1-pro-preview" }), env(), g.upstream);
  assert.ok(g.calls[0].url.includes("/gemini-3.6-flash:"));
});

test("a busy or rate-limited model falls back, and failures don't use up a question", async () => {
  const e = env();
  const g = gemini([429, 503, 200]);
  const res = await handle(ask(), e, g.upstream);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("X-Fixel-Model"), "gemini-3.1-flash-lite");
  const allBusy = await handle(ask(), e, gemini([503, 503, 503, 503]).upstream);
  assert.equal(allBusy.status, 503);
  assert.equal(e.COUNTER.instances.get(today()).storage.data.get(`i:${ID}`), 1);
});

test("a shared network and the whole trial have their own caps", async () => {
  const e = env({ DAILY_PER_IP: "2", DAILY_TOTAL: "3" });
  const other = (n) => ask({ install_id: String(n).repeat(32) }, "9.9.9.9");
  assert.equal((await handle(other(1), e, gemini([]).upstream)).status, 200);
  assert.equal((await handle(other(2), e, gemini([]).upstream)).status, 200);
  assert.equal((await handle(other(3), e, gemini([]).upstream)).status, 429); // same network, new install
  assert.equal((await handle(ask({}, "5.5.5.5"), e, gemini([]).upstream)).status, 200);
  assert.equal((await handle(ask({ install_id: "f".repeat(32) }, "6.6.6.6"), e, gemini([]).upstream)).status, 429); // total
});

test("deep asks for high thinking on Gemini 3", async () => {
  const g = gemini([]);
  await handle(ask({ deep: true }), env(), g.upstream);
  assert.equal(g.calls[0].body.generationConfig.thinkingConfig.thinkingLevel, "high");
});

test("validation rejects anything but text and screenshots", () => {
  const base = { install_id: ID, system: "s" };
  assert.equal(validate({ ...base, contents: [{ role: "user", parts: [{ text: "ok" }] }] }), null);
  assert.equal(validate({ ...base, contents: [{ role: "user", parts: [{ inline_data: { mime_type: "image/png", data: "AA==" } }] }] }), null);
  assert.ok(validate({ ...base, install_id: "../x", contents: [] }));
  assert.ok(validate({ ...base, contents: [{ role: "user", parts: [{ file_data: { file_uri: "gs://x" } }] }] }));
  assert.ok(validate({ ...base, contents: [{ role: "system", parts: [{ text: "x" }] }] }));
  assert.ok(validate({ ...base, contents: [{ role: "user", parts: [{ text: "x", tools: [] }] }] }));
});

test("other paths and methods are refused", async () => {
  assert.equal((await handle(new Request("https://trial.example/"), env(), gemini([]).upstream)).status, 404);
  assert.equal((await handle(new Request("https://trial.example/v1/ask"), env(), gemini([]).upstream)).status, 405);
});

test("parallel requests can't slip past the daily limit", async () => {
  const e = env();
  // Gemini answers after a short wait, so all ten requests are in flight together.
  const slow = async () => { await new Promise((r) => setTimeout(r, 5)); return new Response("data: {}\n\n", { status: 200 }); };
  const results = await Promise.all(Array.from({ length: 10 }, () => handle(ask(), e, slow)));
  assert.equal(results.filter((r) => r.status === 200).length, 3);
});

test("a streamed body over the cap is refused without a Content-Length", async () => {
  const chunk = new Uint8Array(1_000_000).fill(32);
  let sent = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (sent++ < 20) controller.enqueue(chunk); else controller.close();
    },
  });
  const req = new Request("https://trial.example/v1/ask", { method: "POST", body, duplex: "half" });
  assert.equal(req.headers.get("Content-Length"), null);
  const res = await handle(req, env(), gemini([]).upstream);
  assert.equal(res.status, 413);
  assert.ok(sent <= 14, `read ${sent} MB before stopping`);
});
