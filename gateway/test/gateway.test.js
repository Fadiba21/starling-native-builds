import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker, { GatewayState, __test } from "../src/index.js";

class MemoryStorage {
  values = new Map();
  alarmAt = null;
  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, value); }
  async delete(key) { return this.values.delete(key); }
  async list({ prefix = "" } = {}) {
    return new Map([...this.values.entries()].filter(([key]) => key.startsWith(prefix)));
  }
  async setAlarm(at) { this.alarmAt = at; }
}

function fixture() {
  const storage = new MemoryStorage();
  const durableObject = new GatewayState({ storage, blockConcurrencyWhile: (fn) => fn() }, {});
  const namespace = {
    idFromName: (name) => name,
    get: () => ({ fetch: (input, init) => durableObject.fetch(new Request(input, init)) }),
  };
  const env = {
    GATEWAY_STATE: namespace,
    GITHUB_TOKEN: "test-only-github-token",
    UPLOAD_KEY: "test-only-shared-upload-key",
    GITHUB_REPOSITORY: "Fadiba21/starling-native-builds",
    GITHUB_WORKFLOW: "build.yml",
    GITHUB_REF: "main",
    TOOLS_INPUT: "",
  };
  const dispatches = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    dispatches.push({ url: String(url), init, payload: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  };
  return {
    env, storage, dispatches,
    restore: () => { globalThis.fetch = originalFetch; },
  };
}

const bytes = (text) => new TextEncoder().encode(text);
const digest = (data) => createHash("sha256").update(data).digest("hex");
const auth = { authorization: "Bearer test-only-shared-upload-key" };

function bundleBody(text) {
  const file = bytes(text);
  const framed = new Uint8Array(4 + file.length);
  new DataView(framed.buffer).setUint32(0, file.length, false);
  framed.set(file, 4);
  return framed;
}

async function buildRequest(env, body, ip = "198.51.100.1") {
  return worker.fetch(new Request("https://gateway.test/v1/build?version=0.8.0", {
    method: "POST", headers: { "cf-connecting-ip": ip, "content-type": "application/octet-stream" }, body,
  }), env);
}

async function callback(env, ask, payload) {
  return worker.fetch(new Request(`https://gateway.test/v1/internal/job/${ask}/status`, {
    method: "POST", headers: { ...auth, "content-type": "application/json" }, body: JSON.stringify(payload),
  }), env);
}

async function uploadPart(env, ask, index, data) {
  return worker.fetch(new Request(`https://gateway.test/v1/internal/job/${ask}/part/${index}`, {
    method: "PUT", headers: auth, body: data,
  }), env);
}

test("incremental SHA-256 matches standard vectors across split updates", () => {
  const hasher = new __test.Sha256();
  hasher.update(bytes("a")).update(bytes("bc"));
  assert.equal(hasher.hex(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  const large = new Uint8Array(1024 * 1024 + 17).fill(0xa5);
  const streamed = new __test.Sha256().update(large.subarray(0, 333)).update(large.subarray(333));
  assert.equal(streamed.hex(), digest(large));
});

test("source fetch auth, build dispatch, callback auth, verified raw library and Range", async (t) => {
  const f = fixture();
  t.after(f.restore);
  const source = bundleBody("opaque framed native mod bytes");
  const accepted = await buildRequest(f.env, source);
  assert.equal(accepted.status, 200);
  const { job } = await accepted.json();
  assert.match(job, /^[0-9a-f]{24}$/);
  assert.equal(f.dispatches.length, 1);
  const dispatch = f.dispatches[0];
  assert.match(dispatch.url, /repos\/Fadiba21\/starling-native-builds\/actions\/workflows\/build\.yml\/dispatches$/);
  assert.equal(dispatch.payload.ref, "main");
  assert.equal(dispatch.payload.inputs.version, "0.8.0");
  assert.equal(dispatch.payload.inputs.gateway, "https://gateway.test");
  assert.equal(dispatch.payload.inputs.tools, "starling");
  const ask = dispatch.payload.inputs.ask;
  assert.match(ask, /^[0-9a-f]{32}$/);

  const sourceUrl = `https://gateway.test/v1/github/${ask}`;
  const sourceUnauthenticated = await worker.fetch(new Request(sourceUrl), f.env);
  assert.equal(sourceUnauthenticated.status, 401);
  const sourceWrongKey = await worker.fetch(new Request(sourceUrl, { headers: { authorization: "Bearer wrong-key" } }), f.env);
  assert.equal(sourceWrongKey.status, 401);
  const sourceResponse = await worker.fetch(new Request(sourceUrl, { headers: auth }), f.env);
  assert.equal(sourceResponse.status, 200);
  assert.deepEqual(new Uint8Array(await sourceResponse.arrayBuffer()), source);
  assert.equal((await worker.fetch(new Request(`https://gateway.test/v1/github/${"0".repeat(32)}`, { headers: auth }), f.env)).status, 404);

  const unauthorized = await worker.fetch(new Request(`https://gateway.test/v1/internal/job/${ask}/status`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ state: "building" }),
  }), f.env);
  assert.equal(unauthorized.status, 401);

  const building = await callback(f.env, ask, { state: "building" });
  assert.equal(building.status, 200);
  const so = bytes("ELF-native-library");
  const uploaded = await uploadPart(f.env, ask, 0, so);
  assert.equal(uploaded.status, 200);
  assert.equal((await uploaded.json()).completed, false);
  const done = await callback(f.env, ask, {
    state: "done", sha256: digest(so), size: so.length, build: { permissions: ["microphone"] }, parts: 1, chunk_size: __test.CHUNK_BYTES,
  });
  assert.equal(done.status, 200);
  assert.equal((await done.json()).completed, true);
  const status = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}`), f.env);
  assert.deepEqual(await status.json(), {
    state: "done", sha256: digest(so), size: so.length,
    build: { permissions: ["microphone"], size: so.length, sha256: digest(so) },
  });
  const library = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`), f.env);
  assert.equal(library.status, 200);
  assert.deepEqual(new Uint8Array(await library.arrayBuffer()), so);
  const ranged = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`, { headers: { range: "bytes=4-" } }), f.env);
  assert.equal(ranged.status, 206);
  assert.equal(ranged.headers.get("content-range"), `bytes 4-${so.length - 1}/${so.length}`);
  assert.deepEqual(new Uint8Array(await ranged.arrayBuffer()), so.subarray(4));
  const fromZero = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`, { headers: { range: "bytes=0-" } }), f.env);
  assert.equal(fromZero.status, 206);
  assert.equal(fromZero.headers.get("content-range"), `bytes 0-${so.length - 1}/${so.length}`);
  assert.deepEqual(new Uint8Array(await fromZero.arrayBuffer()), so);
  const bounded = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`, { headers: { range: "bytes=2-5" } }), f.env);
  assert.equal(bounded.status, 206);
  assert.equal(bounded.headers.get("content-range"), `bytes 2-5/${so.length}`);
  assert.deepEqual(new Uint8Array(await bounded.arrayBuffer()), so.subarray(2, 6));
  const suffix = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`, { headers: { range: "bytes=-5" } }), f.env);
  assert.equal(suffix.status, 206);
  assert.equal(suffix.headers.get("content-range"), `bytes ${so.length - 5}-${so.length - 1}/${so.length}`);
  assert.deepEqual(new Uint8Array(await suffix.arrayBuffer()), so.subarray(-5));
  for (const invalidRange of ["bytes=999-", "bytes=8-2", "bytes=-0", "bytes=1-2,4-5", "bytes=abc"]) {
    const rejectedRange = await worker.fetch(new Request(`https://gateway.test/v1/job/${job}/lib`, { headers: { range: invalidRange } }), f.env);
    assert.equal(rejectedRange.status, 416, invalidRange);
    assert.equal(rejectedRange.headers.get("content-range"), `bytes */${so.length}`);
  }
  const busy = await worker.fetch(new Request("https://gateway.test/v1/busy"), f.env);
  assert.deepEqual(await busy.json(), { busy: false });
});

test("only one dispatched build runs; at most 3 builds/day and 2 per IP", async (t) => {
  const f = fixture();
  t.after(f.restore);
  const first = await (await buildRequest(f.env, bundleBody("mod-one"), "192.0.2.1")).json();
  const secondResponse = await buildRequest(f.env, bundleBody("mod-two"), "192.0.2.2");
  assert.equal(secondResponse.status, 200);
  const second = await secondResponse.json();
  assert.equal(f.dispatches.length, 1, "second job waits while the first owns the runner slot");
  assert.deepEqual(await (await worker.fetch(new Request("https://gateway.test/v1/busy"), f.env)).json(), { busy: true });

  const ask1 = f.dispatches[0].payload.inputs.ask;
  const result = bytes("one");
  await uploadPart(f.env, ask1, 0, result);
  const done1 = await callback(f.env, ask1, { state: "done", sha256: digest(result), size: result.length, build: {}, parts: 1, chunk_size: __test.CHUNK_BYTES });
  assert.equal(done1.status, 200);
  assert.equal(f.dispatches.length, 2, "completion dispatches the next waiting job");
  assert.equal(f.dispatches[1].payload.inputs.ask !== ask1, true);

  const thirdResponse = await buildRequest(f.env, bundleBody("mod-three"), "192.0.2.3");
  assert.equal(thirdResponse.status, 200);
  const perIpLimit = await buildRequest(f.env, bundleBody("mod-four"), "192.0.2.1");
  assert.equal(perIpLimit.status, 429);
  const dailyLimit = await buildRequest(f.env, bundleBody("mod-five"), "192.0.2.5");
  assert.equal(dailyLimit.status, 429);
  assert.equal(f.dispatches.length, 2);
  assert.equal((await worker.fetch(new Request(`https://gateway.test/v1/job/${first.job}`), f.env)).status, 200);
  assert.equal((await worker.fetch(new Request(`https://gateway.test/v1/job/${second.job}`), f.env)).status, 200);
});

test("unknown versions are refused and failed uploads cannot become done without SHA verification", async (t) => {
  const f = fixture();
  t.after(f.restore);
  const invalidVersion = await worker.fetch(new Request("https://gateway.test/v1/build?version=9.9.9", {
    method: "POST", body: bundleBody("no"),
  }), f.env);
  assert.equal(invalidVersion.status, 400);
  const accepted = await (await buildRequest(f.env, bundleBody("hash-check"))).json();
  const ask = f.dispatches[0].payload.inputs.ask;
  const so = bytes("different bytes");
  await uploadPart(f.env, ask, 0, so);
  const mismatch = await callback(f.env, ask, { state: "done", sha256: "0".repeat(64), size: so.length, build: {}, parts: 1, chunk_size: __test.CHUNK_BYTES });
  assert.equal(mismatch.status, 422);
  const status = await worker.fetch(new Request(`https://gateway.test/v1/job/${accepted.job}`), f.env);
  assert.equal((await status.json()).state, "failed");
});

test("failed upload with unknown final size removes every partial library chunk", async (t) => {
  const f = fixture();
  t.after(f.restore);
  const { job } = await (await buildRequest(f.env, bundleBody("partial-upload"))).json();
  const ask = f.dispatches[0].payload.inputs.ask;
  const partial = bytes("partial library without metadata");
  assert.equal((await uploadPart(f.env, ask, 0, partial)).status, 200);
  assert.ok(f.storage.values.has(`lib:${job}:0`));
  const failed = await callback(f.env, ask, { state: "failed", error: "runner failed" });
  assert.equal(failed.status, 200);
  assert.equal([...f.storage.values.keys()].filter((key) => key.startsWith(`lib:${job}:`)).length, 0);
});

test("stale runner leases are reaped and both public status and busy kick the next job", async (t) => {
  const f = fixture();
  t.after(f.restore);
  const jobs = [];
  for (let i = 0; i < 3; i++) {
    jobs.push(await (await buildRequest(f.env, bundleBody(`lease-${i}`), `192.0.2.${20 + i}`)).json());
  }
  assert.equal(f.dispatches.length, 1);

  const staleBefore = Date.now() - __test.STALE_LEASE_MS - 1;
  const firstRecord = f.storage.values.get(`job:${jobs[0].job}`);
  firstRecord.lease_at = staleBefore;
  await f.storage.put(`job:${jobs[0].job}`, firstRecord);
  const status = await worker.fetch(new Request(`https://gateway.test/v1/job/${jobs[0].job}`), f.env);
  assert.deepEqual(await status.json(), { state: "failed", error: "The build runner timed out before completing this job.", settled: false });
  assert.equal(f.dispatches.length, 2, "status reaps the stale slot and dispatches the next waiting job");

  const secondAsk = f.dispatches[1].payload.inputs.ask;
  const secondRecord = [...f.storage.values.entries()].find(([key, value]) => key.startsWith("job:") && value.ask === secondAsk)[1];
  secondRecord.lease_at = Date.now() - __test.STALE_LEASE_MS - 1;
  await f.storage.put(`job:${secondRecord.id}`, secondRecord);
  const busy = await worker.fetch(new Request("https://gateway.test/v1/busy"), f.env);
  assert.deepEqual(await busy.json(), { busy: true });
  assert.equal(f.dispatches.length, 3, "busy reaps another stale slot and dispatches the remaining queued job");
  const secondStatus = await worker.fetch(new Request(`https://gateway.test/v1/job/${secondRecord.id}`), f.env);
  assert.equal((await secondStatus.json()).error, "The build runner timed out before completing this job.");
});
