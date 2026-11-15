// Fixel's free trial: answers a few questions a day for people who haven't added a Gemini key yet.
//
// The owner's Gemini key lives only here, as the GEMINI_API_KEY secret, never in the app or on GitHub.
// Limits, all per UTC day: DAILY_PER_INSTALL questions per Fixel install, DAILY_PER_IP per network
// (schools share one IP, so it's higher), and DAILY_TOTAL across everyone, which protects the owner's
// own free-tier quota. Only Gemini free-tier models are ever called, whatever the client asks for.
//
// The counts live in one TrialCounter Durable Object per day. A Durable Object handles one request at a
// time, so "check all three limits, then take a question" can't interleave: KV can't do that, and ten
// parallel requests would all read "2 used" and all get through.

const MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"];
const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
const MAX_BODY = 12_000_000; // five ~1568px screenshots, base64, with room to spare
const MAX_TURNS = 40;
const MAX_SYSTEM = 40_000;
const MAX_OUTPUT_TOKENS = 8192;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg"]);
const INSTALL_ID = /^[0-9a-f]{32}$/;

export default {
  async fetch(request, env) {
    try {
      return await handle(request, env, fetch);
    } catch (err) {
      return json(500, { error: "server", message: "The free trial hit a problem. Try again in a minute." });
    }
  },
};

export async function handle(request, env, upstream) {
  const url = new URL(request.url);
  if (url.pathname !== "/v1/ask") return json(404, { error: "not_found" });
  if (request.method !== "POST") return json(405, { error: "method" });

  const raw = await readCapped(request, MAX_BODY);
  if (raw === null) return json(413, { error: "too_big", message: "Those screenshots are too big for the free trial." });

  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json(400, { error: "bad_request", message: "Expected JSON." });
  }
  const problem = validate(body);
  if (problem) return json(400, { error: "bad_request", message: problem });

  const day = new Date().toISOString().slice(0, 10);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const limits = {
    install: intVar(env.DAILY_PER_INSTALL, 3),
    ip: intVar(env.DAILY_PER_IP, 30),
    total: intVar(env.DAILY_TOTAL, 200),
  };
  const keys = { install: `i:${body.install_id}`, ip: `p:${ip}`, total: "t" };
  const counter = env.COUNTER.get(env.COUNTER.idFromName(day));
  const call = async (path, payload) => (await counter.fetch(`https://counter${path}`, { method: "POST", body: JSON.stringify(payload) })).json();

  // Take a question before asking Gemini, and give it back if no model answers.
  const slot = await call("/reserve", { keys, limits });
  if (!slot.ok) {
    if (slot.full === "install") return used(0, `You've used today's ${limits.install} free questions.`);
    if (slot.full === "ip") return used(slot.remaining, "Your network has used today's free questions.");
    return used(slot.remaining, "The free trial is full for today.");
  }
  const refund = () => call("/refund", { keys });

  for (const model of MODELS) {
    const generationConfig = { maxOutputTokens: MAX_OUTPUT_TOKENS };
    if (model.startsWith("gemini-3")) generationConfig.thinkingConfig = { thinkingLevel: body.deep ? "high" : "low" };
    const answer = await upstream(`${GEMINI}/${model}:streamGenerateContent?alt=sse`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: body.contents, systemInstruction: { parts: [{ text: body.system }] }, generationConfig }),
    });
    if (answer.status === 429 || answer.status === 404 || answer.status >= 500) continue; // next free model
    if (!answer.ok) {
      await refund();
      return json(502, { error: "upstream", message: `Gemini said ${answer.status}.` });
    }
    return new Response(answer.body, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        "X-Fixel-Model": model,
        "X-Fixel-Remaining": String(slot.remaining),
      },
    });
  }
  await refund(); // no model answered, so this question doesn't count
  return json(503, { error: "busy", message: "Gemini is busy right now. Wait a minute, then press Enter." });
}

export function validate(body) {
  if (!body || typeof body !== "object") return "Expected a JSON object.";
  if (typeof body.install_id !== "string" || !INSTALL_ID.test(body.install_id)) return "Missing install id.";
  if (typeof body.system !== "string" || body.system.length > MAX_SYSTEM) return "Bad system prompt.";
  if (body.deep !== undefined && typeof body.deep !== "boolean") return "deep must be true or false.";
  const contents = body.contents;
  if (!Array.isArray(contents) || contents.length === 0 || contents.length > MAX_TURNS) return "Bad conversation.";
  for (const turn of contents) {
    if (!turn || (turn.role !== "user" && turn.role !== "model") || !Array.isArray(turn.parts) || turn.parts.length === 0) return "Bad turn.";
    for (const part of turn.parts) {
      const keys = part && typeof part === "object" ? Object.keys(part) : [];
      if (keys.length !== 1) return "Bad part.";
      if (keys[0] === "text" && typeof part.text === "string") continue;
      const data = part.inline_data;
      if (keys[0] === "inline_data" && data && IMAGE_TYPES.has(data.mime_type) && typeof data.data === "string" && Object.keys(data).length === 2) continue;
      return "Only text and PNG/JPEG screenshots are allowed.";
    }
  }
  return null;
}

function intVar(value, fallback) {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

// Reads the body as a stream and stops as soon as it passes `max` bytes, so a huge or endless upload is
// never held in memory. Content-Length is only used to refuse early; it's never trusted to be the real size.
export async function readCapped(request, max) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null && !(/^\d+$/.test(declared) && Number(declared) <= max)) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const c of chunks) {
    bytes.set(c, offset);
    offset += c.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

// One instance per UTC day (idFromName(day)). Counts are loaded once, then every check-and-increment runs
// with no await in between, so two requests can never both take the last question.
export class TrialCounter {
  constructor(state) {
    this.storage = state.storage;
    this.counts = new Map();
    state.blockConcurrencyWhile(async () => {
      this.counts = await this.storage.list();
    });
  }

  async fetch(request) {
    const { pathname } = new URL(request.url);
    const { keys, limits } = await request.json();
    const n = (k) => this.counts.get(k) ?? 0;
    const remaining = () => Math.max(0, limits.install - n(keys.install));

    if (pathname === "/reserve") {
      for (const which of ["install", "ip", "total"]) {
        if (n(keys[which]) >= limits[which]) return Response.json({ ok: false, full: which, remaining: remaining() });
      }
      this.bump(keys, +1);
      // A day's counts are useless after two days, so clear them out then.
      if ((await this.storage.getAlarm()) === null) await this.storage.setAlarm(Date.now() + 2 * 86_400_000);
      return Response.json({ ok: true, remaining: remaining() });
    }
    if (pathname === "/refund") {
      this.bump(keys, -1);
      return Response.json({ ok: true });
    }
    return new Response("not found", { status: 404 });
  }

  bump(keys, by) {
    const changed = {};
    for (const k of Object.values(keys)) {
      const next = Math.max(0, (this.counts.get(k) ?? 0) + by);
      this.counts.set(k, next);
      changed[k] = next;
    }
    this.storage.put(changed); // the output gate holds the response until this is saved
  }

  async alarm() {
    await this.storage.deleteAll();
    this.counts = new Map();
  }
}

function used(remaining, message) {
  return json(429, { error: "trial_used", remaining, message }, { "X-Fixel-Remaining": String(Math.max(0, remaining)) });
}

function json(status, payload, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers },
  });
}
