// Fixel's free trial: answers a few questions a day for people who haven't added a Gemini key yet.
//
// The owner's Gemini key lives only here, as the GEMINI_API_KEY secret, never in the app or on GitHub.
// Limits, all per UTC day: DAILY_PER_INSTALL questions per Fixel install, DAILY_PER_IP per network
// (schools share one IP, so it's higher), and DAILY_TOTAL across everyone, which protects the owner's
// own free-tier quota. Only Gemini free-tier models are ever called, whatever the client asks for.

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

  const length = Number(request.headers.get("Content-Length") || "0");
  if (!Number.isFinite(length) || length > MAX_BODY) return json(413, { error: "too_big", message: "Those screenshots are too big for the free trial." });
  const raw = await request.text();
  if (raw.length > MAX_BODY) return json(413, { error: "too_big", message: "Those screenshots are too big for the free trial." });

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
  const keys = { install: `i:${day}:${body.install_id}`, ip: `p:${day}:${ip}`, total: `t:${day}` };
  const [usedInstall, usedIp, usedTotal] = await Promise.all([count(env, keys.install), count(env, keys.ip), count(env, keys.total)]);

  if (usedInstall >= limits.install) return used(0, `You've used today's ${limits.install} free questions.`);
  if (usedIp >= limits.ip) return used(limits.install - usedInstall, "Your network has used today's free questions.");
  if (usedTotal >= limits.total) return used(limits.install - usedInstall, "The free trial is full for today.");

  for (const model of MODELS) {
    const generationConfig = { maxOutputTokens: MAX_OUTPUT_TOKENS };
    if (model.startsWith("gemini-3")) generationConfig.thinkingConfig = { thinkingLevel: body.deep ? "high" : "low" };
    const answer = await upstream(`${GEMINI}/${model}:streamGenerateContent?alt=sse`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: body.contents, systemInstruction: { parts: [{ text: body.system }] }, generationConfig }),
    });
    if (answer.status === 429 || answer.status === 404 || answer.status >= 500) continue; // next free model
    if (!answer.ok) return json(502, { error: "upstream", message: `Gemini said ${answer.status}.` });
    // Counted only once Gemini has agreed to answer, so failures never use up a free question.
    await Promise.all([bump(env, keys.install, usedInstall), bump(env, keys.ip, usedIp), bump(env, keys.total, usedTotal)]);
    return new Response(answer.body, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        "X-Fixel-Model": model,
        "X-Fixel-Remaining": String(Math.max(0, limits.install - usedInstall - 1)),
      },
    });
  }
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

async function count(env, key) {
  return Number.parseInt((await env.TRIAL.get(key)) || "0", 10) || 0;
}

async function bump(env, key, before) {
  await env.TRIAL.put(key, String(before + 1), { expirationTtl: 2 * 86400 });
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
