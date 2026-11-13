# Starling native build gateway (Cloudflare Workers Free)

This code implements the HTTPS gateway protocol used by the Starling Android native-mod build client and dispatches builds to the public `Fadiba21/starling-native-builds` Actions workflow. The project name is `starling-native-gateway`; Cloudflare Git integration should use the `gateway/` subdirectory as its build root. It is intentionally not deployed from this task and contains no credentials.

## Free-tier architecture

One SQLite-backed Durable Object (`GatewayState`) is bound as `GATEWAY_STATE`. Cloudflare's official Durable Objects pricing docs say Workers Free can use SQLite-backed Durable Objects; the Free tier includes 100,000 DO requests/day, 13,000 GB-s/day, 100,000 SQLite row writes/day, 5 million row reads/day, and 5 GB of SQLite stored data account-wide. The Free-tier Durable Object limits page lists 5 GB account storage (its FAQ also references a 1 GB Free object cap), so this gateway deliberately caps retained payload data at a low level: at most 3 accepted builds/rolling 24 hours, input body at most 32 MiB each, output at most 96 MiB each, one dispatched/running build, and two waiting jobs. Peak nominal payload retention under those limits is about 384 MiB (plus SQLite overhead). Every source and output is chunked into 1 MiB-or-smaller values and is deleted after 24 hours by a Durable Object alarm.

Official references checked 2026-11-13:

- [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [Durable Objects limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
- [Starling native mods protocol](https://github.com/starling-client/starling/blob/starling/docs/starling-native-mods.md)
- [Upstream mod build server](https://raw.githubusercontent.com/starling-client/starling/starling/packaging/android/mod_build_server.py)

No R2, paid Workers plan, card, custom domain, or non-SQLite storage is required. Use the free `workers.dev` hostname. The currently supported version map is intentionally only `0.8.0` -> `ac874422ed8f892505ff73d67ae486ecfee7a516`; add versions only after confirming their workflow/base-cache support.

## Deploy/configure

`wrangler.toml` declares `GatewayState` and the SQLite migration. Point Cloudflare Git integration at this project root (`gateway/` in the repository), deploy to a `workers.dev` hostname, then set Cloudflare Worker secrets (not vars):

```sh
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put UPLOAD_KEY
```

`GITHUB_TOKEN` is a fine-grained token scoped only to `Fadiba21/starling-native-builds` and permitted to invoke `workflow_dispatch` (Actions: write). It is held only by the Worker and is never included in workflow inputs or passed to the compiler.

Set repository Actions secret `GATEWAY_UPLOAD_KEY` to the same independently generated high-entropy value as the Worker `UPLOAD_KEY` secret. The workflow should expose it only to its trusted status/upload callback step after the sandbox exits—not to the sandbox process, build scripts, mod source, or artifact contents. Never print the secret in logs. The Worker cannot set the repository secret itself.

The workflow dispatch is `build.yml` on `main` with exact inputs `ask`, `version`, `gateway`, `tools`. `TOOLS_INPUT` defaults to an empty string; the workflow treats blank as its default toolchain selector. Configure it in `wrangler.toml` only if the workflow defines a non-empty selector.

## API

Android-facing routes retain the upstream shapes:

- `POST /v1/build?version=V`: request body is the Starling framing format (repeated four-byte big-endian bundle length followed by that `.hnmod` ZIP). Returns `{"job":"<24 lowercase hex>"}`. Max request body 32 MiB.
- `GET /v1/job/{ID}`: returns `queued`, `building`, `done`, or `failed`; completed status includes SHA-256, size, and the build metadata object.
- `GET /v1/job/{ID}/lib`: raw `libhorizon_android.so`; accepts open-ended `Range: bytes=N-` and returns 206/Content-Range for valid nonzero offsets.
- `GET /v1/busy`: `{"busy":true|false}`.

The workflow callback/source API and exact request JSON are documented in [INTEGRATION.md](./INTEGRATION.md). Callback status and part uploads require `Authorization: Bearer $UPLOAD_KEY`. `GET /v1/github/{ASK}` deliberately has no auth: `ASK` is a random 128-bit, 32-hex capability (matching the upstream GitHub runner source-fetch design) and expires after 24 hours. Parts are zero-indexed and 1 MiB except the final part. The Worker checks part count, exact chunk lengths, metadata, and the final whole-library SHA-256 before exposing a job as `done`.

## Tests

The no-dependency suite runs with Node 24:

```sh
node --test
```

It uses mocked Durable Object storage and a mocked GitHub dispatch endpoint; it does not contact Cloudflare or GitHub. It checks workflow inputs, temporary source retrieval, upload authentication, hash verification, job status, range downloads, queue serialization, version refusal, and quotas.
