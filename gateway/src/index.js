const CHUNK_BYTES = 1024 * 1024;
const MAX_REQUEST_BYTES = 32 * 1024 * 1024;
const MAX_LIBRARY_BYTES = 96 * 1024 * 1024;
const MAX_QUEUE = 2;
const TTL_MS = 24 * 60 * 60 * 1000;
const STALE_LEASE_MS = 80 * 60 * 1000;
const JOB_ID = /^[0-9a-f]{24}$/;
const ASK_ID = /^[0-9a-f]{32}$/;
const HEX_SHA256 = /^[0-9a-f]{64}$/;
const VERSIONS = Object.freeze({
  "0.8.0": "ac874422ed8f892505ff73d67ae486ecfee7a516",
});

const encoder = new TextEncoder();

function json(value, status = 200, extra = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256(bytes) {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
}

async function boundedBytes(request, maxBytes) {
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > maxBytes) throw new HttpError(413, "Request body is too large.");
  if (!request.body) throw new HttpError(400, "Request body is required.");
  const reader = request.body.getReader();
  const parts = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new HttpError(413, "Request body is too large.");
      }
      parts.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  if (!size) throw new HttpError(400, "Request body is required.");
  const result = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.byteLength;
  }
  return result;
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function storeStub(env) {
  return env.GATEWAY_STATE.get(env.GATEWAY_STATE.idFromName("starling-global-v1"));
}

async function callStore(env, path, init = {}) {
  return storeStub(env).fetch(`https://state.internal${path}`, init);
}

function authorized(request, env) {
  const expected = `Bearer ${env.UPLOAD_KEY || ""}`;
  const provided = request.headers.get("authorization") || "";
  if (!env.UPLOAD_KEY || provided.length !== expected.length) return false;
  let mismatch = 0;
  for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ provided.charCodeAt(i);
  return mismatch === 0;
}

async function dispatchNext(env, origin) {
  const claim = await callStore(env, "/internal/claim");
  const job = await claim.json();
  if (!job.job) return;
  const token = env.GITHUB_TOKEN;
  if (!token) {
    await callStore(env, `/internal/dispatch-result/${job.job}`, {
      method: "POST", body: JSON.stringify({ ok: false, error: "GitHub dispatch is not configured." }),
    });
    return dispatchNext(env, origin);
  }
  const repo = env.GITHUB_REPOSITORY || "Fadiba21/starling-native-builds";
  const workflow = env.GITHUB_WORKFLOW || "build.yml";
  const ref = env.GITHUB_REF || "main";
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "starling-native-build-gateway",
      },
      body: JSON.stringify({
        ref,
        inputs: {
          ask: job.ask,
          version: job.version,
          gateway: origin,
          tools: env.TOOLS_INPUT || "starling",
        },
      }),
    });
  } catch {
    response = null;
  }
  if (response?.status === 204) {
    await callStore(env, `/internal/dispatch-result/${job.job}`, {
      method: "POST", body: JSON.stringify({ ok: true }),
    });
    return;
  }
  await callStore(env, `/internal/dispatch-result/${job.job}`, {
    method: "POST", body: JSON.stringify({ ok: false, error: "GitHub Actions could not accept the build." }),
  });
  // A failed dispatch frees the single runner slot. Continue through bounded queued work.
  return dispatchNext(env, origin);
}

async function publicRoute(request, env, url) {
  const path = url.pathname;
  if (request.method === "POST" && path === "/v1/build") {
    const version = url.searchParams.get("version") || "";
    const commit = VERSIONS[version];
    if (!commit) return json({ error: `The build server has no game for Starling ${version.slice(0, 32)}. Update the game.` }, 400);
    const body = await boundedBytes(request, MAX_REQUEST_BYTES);
    const prefix = encoder.encode(`/v1/build?version=${version}${commit}`);
    const canonical = new Uint8Array(prefix.byteLength + body.byteLength);
    canonical.set(prefix);
    canonical.set(body, prefix.byteLength);
    const id = (await sha256(canonical)).slice(0, 24);
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const ipHash = (await sha256(encoder.encode(ip))).slice(0, 32);
    const result = await callStore(env, "/internal/create", {
      method: "POST",
      headers: {
        "content-type": "application/octet-stream",
        "x-job-id": id,
        "x-version": version,
        "x-commit": commit,
        "x-ip-hash": ipHash,
      },
      body,
    });
    const data = await result.json();
    if (!result.ok) return json(data, result.status);
    if (data.created) await dispatchNext(env, url.origin);
    return json({ job: id });
  }

  if (request.method === "GET" && path === "/v1/busy") {
    const maintenance = await callStore(env, "/internal/reap");
    if ((await maintenance.json()).reaped) await dispatchNext(env, url.origin);
    const response = await callStore(env, "/internal/busy");
    return new Response(response.body, { status: response.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  }

  const statusMatch = /^\/v1\/job\/([0-9a-f]{24})$/.exec(path);
  if (request.method === "GET" && statusMatch) {
    const maintenance = await callStore(env, "/internal/reap");
    if ((await maintenance.json()).reaped) await dispatchNext(env, url.origin);
    const response = await callStore(env, `/internal/job/${statusMatch[1]}`);
    return new Response(response.body, { status: response.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  }

  const libraryMatch = /^\/v1\/job\/([0-9a-f]{24})\/lib$/.exec(path);
  if (request.method === "GET" && libraryMatch) {
    return callStore(env, `/internal/job/${libraryMatch[1]}/lib`, {
      headers: request.headers.get("range") ? { range: request.headers.get("range") } : {},
    });
  }
  return json({ error: "No such thing here." }, 404);
}

async function callbackRoute(request, env, url) {
  const path = url.pathname;
  if (path.startsWith("/v1/internal/") && !authorized(request, env)) return json({ error: "Unauthorized." }, 401);

  const ask = /^\/v1\/internal\/job\/([0-9a-f]{32})\/status$/.exec(path)?.[1];
  if (request.method === "POST" && ask) {
    let body;
    try { body = await request.json(); } catch { return json({ error: "Invalid JSON." }, 400); }
    if (!body || !["building", "failed", "done"].includes(body.state)) return json({ error: "Invalid callback state." }, 400);
    if (body.state === "failed" && typeof body.error !== "string") return json({ error: "A failure reason is required." }, 400);
    if (body.state === "done" && (!Number.isSafeInteger(body.size) || body.size < 1 || body.size > MAX_LIBRARY_BYTES || !HEX_SHA256.test(body.sha256 || "") || body.chunk_size !== CHUNK_BYTES || !Number.isSafeInteger(body.parts) || body.parts !== Math.ceil(body.size / CHUNK_BYTES) || !body.build || typeof body.build !== "object" || Array.isArray(body.build))) return json({ error: "Done status requires valid size, sha256, parts, chunk_size, and build metadata." }, 400);
    if (body.build !== undefined && encoder.encode(JSON.stringify(body.build)).byteLength > 64 * 1024) return json({ error: "Invalid build metadata." }, 400);
    const response = await callStore(env, `/internal/callback-ask/${ask}/status`, { method: "POST", body: JSON.stringify(body) });
    const data = await response.json();
    if (data.completed) await dispatchNext(env, url.origin);
    return json(data, response.status);
  }

  const part = /^\/v1\/internal\/job\/([0-9a-f]{32})\/part\/(\d+)$/.exec(path);
  if (request.method === "PUT" && part) {
    const size = Number(request.headers.get("content-length") || 0);
    if (size > CHUNK_BYTES) return json({ error: `Parts must be at most ${CHUNK_BYTES} bytes.` }, 413);
    const bytes = await boundedBytes(request, CHUNK_BYTES);
    const response = await callStore(env, `/internal/callback-ask/${part[1]}/part/${part[2]}`, { method: "PUT", body: bytes });
    const data = await response.json();
    if (data.completed) await dispatchNext(env, url.origin);
    return json(data, response.status);
  }

  // The random ask id is a short-lived capability, just like the upstream GitHub runner endpoint.
  const source = /^\/v1\/github\/([0-9a-f]{32})$/.exec(path)?.[1];
  if (request.method === "GET" && source) {
    if (!authorized(request, env)) return json({ error: "Unauthorized." }, 401);
    return callStore(env, `/internal/source/${source}`);
  }
  return json({ error: "No such thing here." }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/v1/internal/") || url.pathname.startsWith("/v1/github/")) return await callbackRoute(request, env, url);
      return await publicRoute(request, env, url);
    } catch (error) {
      if (error instanceof HttpError) return json({ error: error.message }, error.status);
      return json({ error: "The gateway could not complete this request." }, 500);
    }
  },
};

/* SQLite-backed Durable Object. It uses the DO key/value API (SQLite's storage backend) so binary
 * data is split into small keys; no individual key approaches Cloudflare's 8 MiB ceiling. */
export class GatewayState {
  constructor(state, env) {
    this.state = state;
    this.storage = state.storage;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/internal/create" && request.method === "POST") return this.create(request);
    if (path === "/internal/claim" && request.method === "GET") return this.claim();
    if (path === "/internal/reap" && request.method === "GET") return this.reapStale();
    const dispatchResult = /^\/internal\/dispatch-result\/([0-9a-f]{24})$/.exec(path);
    if (dispatchResult && request.method === "POST") return this.dispatchResult(dispatchResult[1], request);
    if (path === "/internal/busy" && request.method === "GET") return this.busy();
    const jobRoute = /^\/internal\/job\/([0-9a-f]{24})(\/lib)?$/.exec(path);
    if (jobRoute && request.method === "GET") return jobRoute[2] ? this.library(request, jobRoute[1]) : this.status(jobRoute[1]);
    const sourceRoute = /^\/internal\/source\/([0-9a-f]{32})$/.exec(path);
    if (sourceRoute && request.method === "GET") return this.source(sourceRoute[1]);
    const callbackStatus = /^\/internal\/callback-ask\/([0-9a-f]{32})\/status$/.exec(path);
    if (callbackStatus && request.method === "POST") return this.callbackStatus(callbackStatus[1], request);
    const callbackPart = /^\/internal\/callback-ask\/([0-9a-f]{32})\/part\/(\d+)$/.exec(path);
    if (callbackPart && request.method === "PUT") return this.callbackPart(callbackPart[1], Number(callbackPart[2]), request);
    return json({ error: "No such thing here." }, 404);
  }

  async create(request) {
    const bytes = new Uint8Array(await request.arrayBuffer());
    const id = request.headers.get("x-job-id");
    const version = request.headers.get("x-version");
    const commit = request.headers.get("x-commit");
    const ipHash = request.headers.get("x-ip-hash") || "unknown";
    if (!JOB_ID.test(id || "") || !VERSIONS[version] || VERSIONS[version] !== commit || bytes.length < 1 || bytes.length > MAX_REQUEST_BYTES) return json({ error: "Invalid build request." }, 400);
    return this.state.blockConcurrencyWhile(async () => {
      await this.cleanupExpired();
      const old = await this.storage.get(`job:${id}`);
      if (old) return json({ created: false, job: id });
      const now = Date.now();
      const jobs = await this.storage.list({ prefix: "job:" });
      const activeRecent = Array.from(jobs.values()).filter((job) => job.created_at > now - TTL_MS);
      if (activeRecent.length >= 3) return json({ error: "The build gateway has reached today's build limit. Try again tomorrow." }, 429);
      if (activeRecent.filter((job) => job.ip_hash === ipHash).length >= 2) return json({ error: "You have reached today's build limit. Try again tomorrow." }, 429);
      if (activeRecent.filter((job) => ["waiting", "queued", "dispatching", "building", "uploading"].includes(job.state)).length >= MAX_QUEUE + 1) {
        return json({ error: "The build gateway is full. Try again in a few minutes." }, 429);
      }
      const ask = hex(crypto.getRandomValues(new Uint8Array(16)));
      const count = Math.ceil(bytes.length / CHUNK_BYTES);
      const job = {
        id, ask, version, state: "waiting", created_at: now, expires_at: now + TTL_MS,
        ip_hash: ipHash, source_bytes: bytes.length, source_chunks: count,
        library_size: null, library_sha256: null, build: null, error: null, started_at: null,
      };
      await this.storage.put(`job:${id}`, job);
      for (let i = 0; i < count; i++) {
        await this.storage.put(`src:${ask}:${i}`, bytes.slice(i * CHUNK_BYTES, (i + 1) * CHUNK_BYTES));
      }
      await this.scheduleCleanup();
      return json({ created: true, job: id });
    });
  }

  async claim() {
    return this.state.blockConcurrencyWhile(async () => {
      await this.cleanupExpired();
      await this.reapStaleUnlocked();
      const jobs = Array.from((await this.storage.list({ prefix: "job:" })).values()).sort((a, b) => a.created_at - b.created_at);
      if (jobs.some((job) => ["dispatching", "queued", "building", "uploading"].includes(job.state))) return json({ job: null });
      const next = jobs.find((job) => job.state === "waiting");
      if (!next) return json({ job: null });
      next.state = "dispatching";
      next.lease_at = Date.now();
      await this.storage.put(`job:${next.id}`, next);
      return json({ job: next.id, ask: next.ask, version: next.version });
    });
  }

  async dispatchResult(jobId, request) {
    const job = await this.storage.get(`job:${jobId}`);
    if (!job) return json({ error: "No such job." }, 404);
    let result;
    try { result = await request.json(); } catch { return json({ error: "Invalid JSON." }, 400); }
    if (result.ok) {
      if (job.state === "dispatching") { job.state = "queued"; job.lease_at = Date.now(); }
    } else {
      job.state = "failed";
      job.error = result.error || "GitHub Actions could not accept the build.";
      await this.deleteLibraryChunks(job);
    }
    await this.storage.put(`job:${jobId}`, job);
    return json({ ok: true });
  }

  async status(id) {
    await this.reapStale();
    const job = await this.storage.get(`job:${id}`);
    if (!job || job.expires_at <= Date.now()) return json({ error: "No such job." }, 404);
    if (["waiting", "queued", "dispatching"].includes(job.state)) {
      const jobs = Array.from((await this.storage.list({ prefix: "job:" })).values());
      const ahead = jobs.filter((other) => other.created_at < job.created_at && ["waiting", "queued", "dispatching"].includes(other.state)).length;
      return json({ state: "queued", ahead });
    }
    if (job.state === "building" || job.state === "uploading") return json({ state: "building", seconds: Math.max(0, Math.floor((Date.now() - (job.started_at || job.created_at)) / 1000)) });
    if (job.state === "failed") return json({ state: "failed", error: job.error || "The build failed.", settled: false });
    if (job.state === "done") return json({ state: "done", sha256: job.library_sha256, size: job.library_size, build: job.build || { sha256: job.library_sha256, size: job.library_size } });
    return json({ error: "No such job." }, 404);
  }

  async busy() {
    await this.reapStale();
    const jobs = Array.from((await this.storage.list({ prefix: "job:" })).values());
    return json({ busy: jobs.some((job) => ["waiting", "queued", "dispatching", "building", "uploading"].includes(job.state)) });
  }

  async source(ask) {
    const job = (await this.storage.list({ prefix: "job:" })).values();
    let found;
    for (const item of job) if (item.ask === ask) { found = item; break; }
    if (!found || found.expires_at <= Date.now()) return json({ error: "No such source." }, 404);
    let index = 0;
    const storage = this.storage;
    const stream = new ReadableStream({
      async pull(controller) {
        if (index >= found.source_chunks) { controller.close(); return; }
        const chunk = await storage.get(`src:${ask}:${index++}`);
        if (!chunk) { controller.error(new Error("Missing source chunk.")); return; }
        controller.enqueue(new Uint8Array(chunk));
      },
    });
    return new Response(stream, { headers: { "content-type": "application/octet-stream", "content-length": String(found.source_bytes), "cache-control": "no-store" } });
  }

  async library(request, id) {
    const job = await this.storage.get(`job:${id}`);
    if (!job || job.state !== "done" || job.expires_at <= Date.now()) return json({ error: "No such thing here." }, 404);
    const total = job.library_size;
    const requestedRange = request.headers.get("range");
    let start = 0;
    let end = total - 1;
    let partial = false;
    if (requestedRange !== null) {
      const range = /^bytes=(\d*)-(\d*)$/.exec(requestedRange);
      if (!range || (!range[1] && !range[2])) return this.rangeNotSatisfiable(total);
      const first = range[1] ? Number(range[1]) : null;
      const last = range[2] ? Number(range[2]) : null;
      if ((first !== null && !Number.isSafeInteger(first)) || (last !== null && !Number.isSafeInteger(last))) return this.rangeNotSatisfiable(total);
      if (first === null) {
        if (last === 0) return this.rangeNotSatisfiable(total);
        start = Math.max(0, total - last);
      } else {
        if (first >= total || (last !== null && last < first)) return this.rangeNotSatisfiable(total);
        start = first;
        end = last === null ? total - 1 : Math.min(last, total - 1);
      }
      partial = true;
    }
    let offset = start;
    const storage = this.storage;
    const count = Math.ceil(total / CHUNK_BYTES);
    const stream = new ReadableStream({
      async pull(controller) {
        if (offset > end) { controller.close(); return; }
        const index = Math.floor(offset / CHUNK_BYTES);
        const within = offset % CHUNK_BYTES;
        const value = await storage.get(`lib:${id}:${index}`);
        if (!value) { controller.error(new Error("Missing library chunk.")); return; }
        const bytes = new Uint8Array(value);
        const takeEnd = Math.min(bytes.byteLength, total - offset + within, end - offset + within + 1);
        controller.enqueue(bytes.slice(within, takeEnd));
        offset += takeEnd - within;
        if (index >= count) controller.close();
      },
    });
    const headers = new Headers({ "content-type": "application/octet-stream", "content-length": String(end - start + 1), "cache-control": "no-store", "accept-ranges": "bytes" });
    if (partial) headers.set("content-range", `bytes ${start}-${end}/${total}`);
    return new Response(stream, { status: partial ? 206 : 200, headers });
  }

  rangeNotSatisfiable(total) {
    return new Response(null, { status: 416, headers: { "content-range": `bytes */${total}`, "accept-ranges": "bytes", "content-length": "0", "cache-control": "no-store" } });
  }

  async findJobByAsk(ask) {
    for (const job of (await this.storage.list({ prefix: "job:" })).values()) if (job.ask === ask) return job;
    return null;
  }

  async callbackStatus(ask, request) {
    let body;
    try { body = await request.json(); } catch { return json({ error: "Invalid JSON." }, 400); }
    return this.state.blockConcurrencyWhile(async () => {
      const job = await this.findJobByAsk(ask);
      if (!job || job.expires_at <= Date.now()) return json({ error: "No such job." }, 404);
      if (!["waiting", "dispatching", "queued", "building", "uploading", "done"].includes(job.state)) return json({ error: "Job is already terminal." }, 409);
      if (body.state === "building") {
        if (!job.started_at) job.started_at = Date.now();
        if (job.state !== "uploading") job.state = "building";
        job.lease_at = Date.now();
      } else if (body.state === "failed") {
        job.state = "failed";
        job.error = String(body.error).replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 600) || "The build failed.";
        await this.storage.put(`job:${job.id}`, job);
        await this.deleteLibraryChunks(job);
        return json({ ok: true, completed: true });
      } else if (body.state === "done") {
        if (!Number.isSafeInteger(body.size) || body.size < 1 || body.size > MAX_LIBRARY_BYTES || !HEX_SHA256.test(body.sha256 || "") || body.chunk_size !== CHUNK_BYTES || !Number.isSafeInteger(body.parts) || body.parts !== Math.ceil(body.size / CHUNK_BYTES)) return json({ error: "Invalid final library metadata." }, 400);
        if (job.library_size !== null && (job.library_size !== body.size || job.library_sha256 !== body.sha256 || job.parts !== body.parts)) return json({ error: "Library metadata cannot change." }, 409);
        if (job.state === "done") return json({ ok: true, completed: true });
        job.library_size = body.size;
        job.library_sha256 = body.sha256;
        job.parts = body.parts;
        job.chunk_size = body.chunk_size;
        job.build = { ...(body.build || {}), size: body.size, sha256: body.sha256 };
        job.finalize_requested = true;
        job.state = "uploading";
        job.lease_at = Date.now();
      }
      if (!job.started_at) job.started_at = Date.now();
      if (job.finalize_requested) {
        const verified = await this.verifyLibrary(job);
        if (verified === true) return json({ ok: true, completed: true });
        if (verified === false) return json({ error: job.error, completed: true }, 422);
      }
      await this.storage.put(`job:${job.id}`, job);
      return json({ ok: true, completed: false });
    });
  }

  async callbackPart(ask, index, request) {
    if (!Number.isSafeInteger(index) || index < 0 || index >= Math.ceil(MAX_LIBRARY_BYTES / CHUNK_BYTES)) return json({ error: "Invalid part index." }, 400);
    const bytes = new Uint8Array(await request.arrayBuffer());
    if (!bytes.length || bytes.length > CHUNK_BYTES) return json({ error: "Parts must be 1 MiB or smaller." }, 400);
    return this.state.blockConcurrencyWhile(async () => {
      const job = await this.findJobByAsk(ask);
      if (!job || job.expires_at <= Date.now() || !["waiting", "dispatching", "queued", "building", "uploading"].includes(job.state)) return json({ error: "No active upload for this job." }, 404);
      if (job.parts !== undefined && (index >= job.parts || bytes.length !== (index === job.parts - 1 ? job.library_size - index * CHUNK_BYTES : CHUNK_BYTES))) return json({ error: "Part count or length does not match final metadata." }, 400);
      const key = `lib:${job.id}:${index}`;
      const existing = await this.storage.get(key);
      if (existing) {
        const old = new Uint8Array(existing);
        if (old.length !== bytes.length || old.some((value, i) => value !== bytes[i])) return json({ error: "A different part already occupies this index." }, 409);
      } else {
        await this.storage.put(key, bytes);
      }
      job.lease_at = Date.now();
      await this.storage.put(`job:${job.id}`, job);
      if (job.finalize_requested) {
        const verified = await this.verifyLibrary(job);
        if (verified === true) return json({ ok: true, received: bytes.length, completed: true });
        if (verified === false) return json({ error: job.error, completed: true }, 422);
      }
      return json({ ok: true, received: bytes.length, completed: false });
    });
  }

  async verifyLibrary(job) {
    const hasher = new Sha256();
    let total = 0;
    for (let i = 0; i < job.parts; i++) {
      const value = await this.storage.get(`lib:${job.id}:${i}`);
      if (!value) return null;
      const bytes = new Uint8Array(value);
      const expected = i === job.parts - 1 ? job.library_size - i * CHUNK_BYTES : CHUNK_BYTES;
      if (bytes.length !== expected) {
        job.state = "failed";
        job.error = "An uploaded part has the wrong length.";
        await this.storage.put(`job:${job.id}`, job);
        await this.deleteLibraryChunks(job);
        return false;
      }
      total += bytes.length;
      hasher.update(bytes);
    }
    if (total !== job.library_size || hasher.hex() !== job.library_sha256) {
      job.state = "failed";
      job.error = "The uploaded library failed its SHA-256 check.";
      await this.storage.put(`job:${job.id}`, job);
      await this.deleteLibraryChunks(job);
      return false;
    }
    job.state = "done";
    await this.storage.put(`job:${job.id}`, job);
    return true;
  }

  async cleanupExpired() {
    const now = Date.now();
    const jobs = await this.storage.list({ prefix: "job:" });
    for (const job of jobs.values()) {
      if (job.expires_at <= now) await this.deleteJob(job);
    }
  }

  async reapStale() {
    return this.state.blockConcurrencyWhile(async () => {
      return json({ reaped: await this.reapStaleUnlocked() });
    });
  }

  async reapStaleUnlocked() {
    const now = Date.now();
    let reaped = false;
    const jobs = await this.storage.list({ prefix: "job:" });
    for (const job of jobs.values()) {
      if (!job || job.expires_at <= now || !["dispatching", "queued", "building", "uploading"].includes(job.state)) continue;
      const lastActivity = job.lease_at || job.started_at || job.created_at;
      if (!Number.isFinite(lastActivity) || now - lastActivity < STALE_LEASE_MS) continue;
      job.state = "failed";
      job.error = "The build runner timed out before completing this job.";
      job.failed_at = now;
      await this.storage.put(`job:${job.id}`, job);
      await this.deleteLibraryChunks(job);
      reaped = true;
    }
    return reaped;
  }

  async deleteLibraryChunks(job) {
    const chunks = await this.storage.list({ prefix: `lib:${job.id}:` });
    for (const key of chunks.keys()) await this.storage.delete(key);
  }

  async deleteJob(job) {
    for (let i = 0; i < job.source_chunks; i++) await this.storage.delete(`src:${job.ask}:${i}`);
    await this.deleteLibraryChunks(job);
    await this.storage.delete(`job:${job.id}`);
  }

  async scheduleCleanup() {
    const jobs = await this.storage.list({ prefix: "job:" });
    const expiries = Array.from(jobs.values()).map((job) => job.expires_at);
    if (expiries.length) await this.storage.setAlarm(Math.min(...expiries));
  }

  async alarm() {
    await this.state.blockConcurrencyWhile(async () => {
      await this.cleanupExpired();
      await this.reapStaleUnlocked();
      await this.scheduleCleanup();
    });
  }
}

// Small incremental SHA-256 implementation so a result is verified without buffering the entire
// .so file in memory. It is only used over bounded 1 MiB chunks uploaded by the workflow.
class Sha256 {
  constructor() {
    this.h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.k = SHA256_K;
    this.buffer = new Uint8Array(64);
    this.buffered = 0;
    this.bytes = 0;
    this.finished = false;
  }
  update(input) {
    if (this.finished) throw new Error("Hash already finalized");
    const data = input instanceof Uint8Array ? input : new Uint8Array(input);
    this.bytes += data.length;
    let at = 0;
    if (this.buffered) {
      const take = Math.min(64 - this.buffered, data.length);
      this.buffer.set(data.subarray(0, take), this.buffered);
      this.buffered += take;
      at += take;
      if (this.buffered === 64) { this.block(this.buffer); this.buffered = 0; }
    }
    while (at + 64 <= data.length) { this.block(data.subarray(at, at + 64)); at += 64; }
    if (at < data.length) { this.buffer.set(data.subarray(at), 0); this.buffered = data.length - at; }
    return this;
  }
  block(b) {
    const w = new Uint32Array(64);
    for (let i = 0; i < 16; i++) w[i] = ((b[i * 4] << 24) | (b[i * 4 + 1] << 16) | (b[i * 4 + 2] << 8) | b[i * 4 + 3]) >>> 0;
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      const s0 = (rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)) >>> 0;
      const s1 = (rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10)) >>> 0;
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b0, c, d, e, f, g, h] = this.h;
    for (let i = 0; i < 64; i++) {
      const s1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) >>> 0;
      const ch = ((e & f) ^ (~e & g)) >>> 0;
      const t1 = (h + s1 + ch + this.k[i] + w[i]) >>> 0;
      const s0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) >>> 0;
      const maj = ((a & b0) ^ (a & c) ^ (b0 & c)) >>> 0;
      const t2 = (s0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b0; b0 = a; a = (t1 + t2) >>> 0;
    }
    const v = [a, b0, c, d, e, f, g, h];
    for (let i = 0; i < 8; i++) this.h[i] = (this.h[i] + v[i]) >>> 0;
  }
  digest() {
    if (!this.finished) {
      const bitLength = BigInt(this.bytes) * 8n;
      this.buffer[this.buffered++] = 0x80;
      if (this.buffered > 56) { this.buffer.fill(0, this.buffered); this.block(this.buffer); this.buffered = 0; }
      this.buffer.fill(0, this.buffered, 56);
      for (let i = 0; i < 8; i++) this.buffer[63 - i] = Number((bitLength >> BigInt(i * 8)) & 0xffn);
      this.block(this.buffer);
      this.finished = true;
    }
    const out = new Uint8Array(32);
    this.h.forEach((v, i) => { out[i * 4] = v >>> 24; out[i * 4 + 1] = v >>> 16; out[i * 4 + 2] = v >>> 8; out[i * 4 + 3] = v; });
    return out;
  }
  hex() { return hex(this.digest()); }
}

function rotr(value, bits) { return (value >>> bits) | (value << (32 - bits)); }
const SHA256_K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]);

export const __test = { Sha256, CHUNK_BYTES, MAX_REQUEST_BYTES, MAX_LIBRARY_BYTES, STALE_LEASE_MS, VERSIONS, boundedBytes };
