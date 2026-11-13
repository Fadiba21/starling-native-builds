# Starling native build gateway (Cloudflare Workers Free)

This worker implements the HTTPS gateway used by Starling's Android native-mod client and dispatches builds to the public `Fadiba21/starling-native-builds` Actions workflow. Configure Cloudflare Git integration to use `gateway/` as the project root, or deploy from that directory with Wrangler. The source contains no credentials.

## Free-tier architecture and safeguards

The service uses one SQLite-backed Durable Object (`GATEWAY_STATE`) and a `workers.dev` hostname. Cloudflare's current [Durable Objects limits](https://developers.cloudflare.com/durable-objects/platform/limits/) allow 5 GB of SQLite-backed Durable Object storage per Free account. The [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) include 100,000 requests/day, 10 ms CPU per request, and 128 MB memory on the Free plan.

There is no rolling daily build-count cap, per-IP build cap, or fixed queue-length cap. Builds are accepted until the gateway's retained-payload reservation reaches a nominal 4.5 GB, leaving 0.5 GB of the account's Free storage quota as headroom. Platform storage overhead, other account usage, and per-object enforcement can exhaust actual capacity earlier; this reservation is not a guarantee of available storage. A full storage reservation returns a temporary `429`; expired job data is deleted after 24 hours. The worker dispatches up to 14 builds concurrently; additional accepted jobs wait for a runner slot. Each input is limited to 32 MiB to keep the request processing within the Free Worker memory budget, and each built library may be up to 256 MiB, matching the workflow uploader. Source and output data are stored in 1 MiB-or-smaller chunks.

The existing Durable Object's alarm retries idle waiting work after 30 seconds and reaps stale runner leases even without a connected client. Every dispatch batch remains bounded to 14 attempts; a run of GitHub dispatch failures therefore cannot strand the remaining accepted queue indefinitely. A rejected dispatch is still reported as a failed job, not silently treated as a successful build.

The 14-runner limit is below the [20 concurrent standard GitHub-hosted jobs available on GitHub Free](https://docs.github.com/en/actions/reference/limits) and keeps this dispatcher's Worker subrequests within the Free invocation budget. Standard GitHub-hosted runner usage is free for public repositories ([GitHub Actions billing](https://docs.github.com/en/actions/administering-github-actions/usage-limits-billing-and-administration)). The build workflow itself retains a 70-minute per-job timeout; the trusted cold base build retains its 300-minute timeout.

Official references:

- [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [Durable Objects limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)
- [Starling native mods protocol](https://github.com/starling-client/starling/blob/starling/docs/starling-native-mods.md)
- [Upstream mod build server](https://raw.githubusercontent.com/starling-client/starling/starling/packaging/android/mod_build_server.py)

## Deploy and configure

`wrangler.toml` declares the `GatewayState` Durable Object and its SQLite migration. Set the Worker secrets (not vars) in Cloudflare:

```sh
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put UPLOAD_KEY
```

`GITHUB_TOKEN` should be a fine-grained token scoped only to `Fadiba21/starling-native-builds`, with permission to invoke `workflow_dispatch` (Actions: write). It is held only by the Worker and is never included in workflow inputs or passed to the compiler.

Set the repository Actions secret `GATEWAY_UPLOAD_KEY` to the same independently generated high-entropy value as the Worker `UPLOAD_KEY`. The workflow exposes it only to the trusted source-fetch and callback/upload steps—not to the compiler sandbox, build scripts, mod source, or artifact contents. Never print the secret in logs. The Worker cannot set the repository secret itself.

The workflow dispatch is `build.yml` on `main`, with exact inputs `ask`, `version`, `gateway`, and `tools`. `TOOLS_INPUT` defaults to an empty string; the workflow treats blank as its default toolchain selector.

## API

- `GET /` and `GET /health`: return `{"ok":true,"service":"starling-native-gateway"}`.
- `POST /v1/build?version=V`: accepts the Starling framing format (repeated four-byte big-endian bundle length followed by that `.hnmod` ZIP), up to 32 MiB, and returns `{"job":"<24 lowercase hex>"}`.
- `GET /v1/job/{ID}`: returns `queued`, `building`, `done`, or `failed`; completed status includes SHA-256, size, and build metadata.
- `GET /v1/job/{ID}/lib`: returns the raw `libhorizon_android.so`; `Range: bytes=0-` is treated as a full `200` response so Android can create a fresh `.part` file, while resume ranges from nonzero offsets return `206`.
- `GET /v1/busy`: returns `{"busy":true|false}`.

The root and health routes prove Worker reachability only. Use `/v1/busy` to exercise Durable Object access, and a synthetic end-to-end build to verify GitHub dispatch, compilation, callback, and download readiness.

Workflow source fetch and callbacks require `Authorization: Bearer $UPLOAD_KEY`. The source route returns the exact stored request bytes. The random 128-bit callback key expires with the job after 24 hours. This is a public GitHub repository: dispatch inputs, including `ask`, and workflow logs are not private. The worker validates uploaded part counts, exact chunk lengths, build metadata, and the final whole-library SHA-256 before marking a result `done`.

## Tests

Run the no-dependency mocked suite with Node 24:

```sh
node --test
```

Tests use mocked Durable Object storage and mocked GitHub dispatch; they do not contact Cloudflare or GitHub and do not use a personal mod source. They cover the health route, queueing beyond the former count caps, 14-way dispatch, alarm recovery after repeated dispatch failures, the retained-storage guard, source and callback authentication, hash verification, job status, and range downloads.
