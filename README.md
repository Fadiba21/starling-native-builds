# Starling native build workflow

This repository draft adapts the public [`starling-client/mod-builds`](https://github.com/starling-client/mod-builds) source (upstream snapshot `869989193102c33a061314eabf5b0a7b6c3442b3`) for the public `Fadiba21/starling-native-builds` repository. The trusted base-cache workflow and upstream `scripts/`/`versions.json` are retained. `build.yml` keeps the upstream `mod_build_once.py` → `mod_build_sandbox.sh` route; native mod compilation runs in the upstream isolated, no-network sandbox. Checkout does not persist credentials, repository permissions are read-only, and `GATEWAY_UPLOAD_KEY` is scoped to the trusted source-fetch and post-build callback/upload steps, never the compiler sandbox.

Before each sandboxed build, the trusted runner installs narrow compatibility changes into the pinned source and adapter. It preserves Horizonmatica 0.3.2's exact old-to-current target aliases and migrates verified 0.8.0 API changes used by audited packages: the `mob_hit()` tuple shape, dynamic-light selection helper, option-label formatter, legacy `ui::action_button`/`widgets::Action::Done` drawing API, and the visibility of `Game::menu_slot`. The Done-button shim only restores drawing; each mod's own screen input handler still dispatches its action. The mod bundle, hook kinds, and bindings are left unchanged. These compatibility patches do not certify runtime behavior; successful native compilation remains the check for each package set.

## Gateway interface implemented

`workflow_dispatch` takes `ask`, `version`, `gateway`, and `tools`. The ask must be exactly 32 lowercase hexadecimal characters; version must be present in `versions.json`; gateway must be an HTTPS origin without a path/query; and `tools` must be a valid upstream Git branch name. Values are read from environment variables and validated before they're used. `GET /v1/github/{ASK}` returns the original binary mods input only with `Authorization: Bearer $GATEWAY_UPLOAD_KEY`; the fetch step receives the secret locally and does not pass it into compilation.

After the compiler step (including on earlier preparation/cache failures), the callback step uses `Authorization: Bearer $GATEWAY_UPLOAD_KEY`:

- `POST /v1/internal/job/{ASK}/status` with `{"state":"building"}` immediately before transfer.
- `PUT /v1/internal/job/{ASK}/part/{N}` with raw `application/octet-stream` bytes, zero-indexed `N`, and chunks of 1,048,576 bytes (the final chunk may be smaller).
- `POST /v1/internal/job/{ASK}/status` with `{"state":"done","sha256":"…","size":N,"build":{…},"parts":N,"chunk_size":1048576}` after local hash/size verification and all parts are accepted.
- For build/pre-build failures, `POST` the official `{"state":"failed","error":"…","settled":false}` shape (the upstream `settled` value is retained for native compile failures). Error text for infrastructure/pre-build failures is generic.

All callback attempts are best effort; a one-day Actions result artifact is retained as fallback. The source-fetch and post-build steps do not print the bearer key or follow redirects, and neither exposes the key to the compiler. This is still a public GitHub Actions repository: `workflow_dispatch` inputs including `ask` remain visible to repository viewers, and workflow logs must not be treated as private. The run name omits `ask` to avoid duplicating it there, but the authenticated gateway source route is not a private service boundary. The tests use a mocked HTTP transport and fixture library bytes; they do not run an expensive native build or require a secret.

## Base cache and testing

The current supported game version is `0.8.0`. Run the trusted `base` workflow once per supported release before dispatching builds. A cold base build is expected to take about four hours; `base.yml` has the upstream 300-minute timeout. The per-mod build workflow retains the upstream 70-minute job timeout.

Run the local mocked tests with:

```sh
python3 -m unittest discover -s tests -v
```

The test workflow runs only input-validation and mocked upload tests. It does not restore build caches, invoke the game compiler, use secrets, or contact the gateway.

## Cache keepalive
The `cache keepalive` workflow is configured for 03:17 UTC every Tuesday and Friday and can also be run with `workflow_dispatch`. It only restores the existing trusted `base-COMMIT` cache; it does not build native mods or save or repair caches. If the cache is evicted or missing, run the manual `base` workflow for version `0.8.0` with tools branch `starling` to rebuild it.
