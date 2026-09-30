# Validation and release evidence

The source publication carries **preview.10** implementation and a sanitized
[acceptance summary](../validation/preview.10.json). The summary retains case
names, statuses, expected backend sets, timings and SHA256 of each original
private report. Raw infrastructure logs and deployment identities are excluded.
The hashes establish provenance; the original private reports are not public.

| Suite | Result | What it establishes |
| --- | --- | --- |
| AMD64 native SR + Envoy, mock backends | 48 passed | Routing, negative cases, streams, 14 MLX protocol cases |
| ARM64 native SR + Envoy, mock backends | 48 passed | Same case/status/expected-backend outcomes on ARM64 |
| AMD64 real text/cloud | 8 passed | Local text, SSE, cloud text/vision, authentication and scope |
| ARM64 real external-text/cloud | 8 passed | Cross-host text plus cloud routing |
| H3 real short video | 3 passed | Local video generation and two expected scope/model refusals |
| H3 after MLX restart | 3 passed | Real generation after service restart |

The H3 sample was 256×256, 22 frames at 24 FPS, four turbo steps, with 32 kHz
stereo PCM. One measured gateway generation took about 12.6 seconds and first
progress arrived in about 72 ms. These are single-fixture observations, not a
benchmark or a throughput guarantee. The fixture also passed before/after a
service restart. Full host reboot, long video, Ref2VA and concurrent load were not
accepted by these checks.

Build artifact records: [AMD64](../validation/build-amd64.json) and
[ARM64](../validation/build-arm64.json). The referenced image digests identify
locally built/imported OCI artifacts. **They are not public registry downloads.**
Build and load your own image before deployment; update the deployment contract
to its immutable reference and perform explicit mock acceptance before real use.

Earlier preview.9 full matrices and TTS/WebSocket experiments are historical
baseline evidence. They were not rerun as a full matrix for preview.10. Public
GitHub Actions checks source integrity, portable configuration and unit behavior;
it does not create a Kubernetes cluster, start GPUs, or call a paid model API.
Changing example hostnames does not constitute a new real-host acceptance run.
