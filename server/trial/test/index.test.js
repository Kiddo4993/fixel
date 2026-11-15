// node --test server/trial/test  (no installs needed: Node 20+'s built-in runner)
import assert from "node:assert/strict";
import { test } from "node:test";

import { handle, validate } from "../src/index.js";

const ID = "0123456789abcdef0123456789abcdef";

function fakeKV() {
  const store = new Map();
  return { store, get: async (k) => store.get(k) ?? null, put: async (k, v) => void store.set(k, v) };
}

function env(overrides = {}) {
  return { GEMINI_API_KEY: "secret-key", TRIAL: fakeKV(), ...overrides };
}

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
  assert.equal(e.TRIAL.store.get(`i:${new Date().toISOString().slice(0, 10)}:${ID}`), "1");
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
