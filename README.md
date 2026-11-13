# Starling native build workflow

This repository draft adapts the public [`starling-client/mod-builds`](https://github.com/starling-client/mod-builds) source (upstream snapshot `869989193102c33a061314eabf5b0a7b6c3442b3`) for the public `Fadiba21/starling-native-builds` repository. The trusted base-cache workflow and upstream `scripts/`/`versions.json` are retained. `build.yml` keeps the upstream `mod_build_once.py` → `mod_build_sandbox.sh` route; native mod compilation runs in the upstream isolated, no-network sandbox. Checkout does not persist credentials, repository permissions are read-only, and `GATEWAY_UPLOAD_KEY` is scoped exclusively to the post-build callback/upload step.

## Gateway interface implemented

`workflow_dispatch` takes `ask`, `version`, `gateway`, and `tools`. The ask must be exactly 32 lowercase hexadecimal characters; version must be present in `versions.json`; gateway must be an HTTPS origin without a path/query; and `tools` must be a valid upstream Git branch name. Values are read from environment variables and validated before they're used. `GET /v1/github/{ASK}` returns the binary mods input.

After the compiler step (including on earlier preparation/cache failures), the callback step uses `Authorization: Bearer $GATEWAY_UPLOAD_KEY`:

- `POST /v1/internal/job/{ASK}/status` with `{"state":"building"}` immediately before transfer.
- `PUT /v1/internal/job/{ASK}/part/{N}` with raw `application/octet-stream` bytes, zero-indexed `N`, and chunks of 1,048,576 bytes (the final chunk may be smaller).
- `POST /v1/internal/job/{ASK}/status` with `{"state":"done","sha256":"…","size":N,"build":{…},"parts":N,"chunk_size":1048576}` after local hash/size verification and all parts are accepted.
- For build/pre-build failures, `POST` the official `{"state":"failed","error":"…","settled":false}` shape (the upstream `settled` value is retained for native compile failures). Error text for infrastructure/pre-build failures is generic.

All callback attempts are best effort; a one-day Actions result artifact is retained as fallback. The post-build step never prints the bearer key, does not follow redirects, and does not expose the key to the compiler. The tests use a mocked HTTP transport and fixture library bytes; they do not run an expensive native build or require a secret.

## Base cache and testing

The current supported game version is `0.8.0`. Run the trusted `base` workflow once per supported release before dispatching builds. A cold base build is expected to take about four hours; `base.yml` has the upstream 300-minute timeout. The per-mod build workflow retains the upstream 70-minute job timeout.

Run the local mocked tests with:

```sh
python3 -m unittest discover -s tests -v
```

The test workflow runs only input-validation and mocked upload tests. It does not restore build caches, invoke the game compiler, use secrets, or contact the gateway.
