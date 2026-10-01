# Changelog

## Unreleased

- Add native cloud/local embedding request routing, vector-space isolation, batch/dimension limits, and text/token/multimodal protocol preservation.
- Add Ark multimodal embedding adaptation and the pinned Qwen3-VL-Embedding-2B host engine, unified host settings, and validated preview.11 artifacts.
- Validate 39 embedding gateway cases on each architecture, 17 real embedding/chat cases, eight existing chat/vision cases and three H3 cases; retain older full-media evidence separately.

- Add one schema-validated `stack.yaml` input for deployment, credentials, tests
  and explicit MLX host tooling; keep legacy CLI compatibility and a migration command.
- Separate shared backend settings from model capabilities and enable independent backend keys.
- Add offline change plans, successful-deployment snapshots and complete generated-file replacement.
- Add nine portable stack examples, release-contract import, configuration guides
  and migration/equivalence tests. No new GPU or real-model acceptance is claimed.


## 0.1.0-preview.10 — initial public source publication

- Native MLX-Serve H3 video task, codec, capability filtering, and dispatch.
- External text/video backend registration and per-model gateway timeouts.
- AMD64 and ARM64 source/build provenance and gateway validation summaries.
- Public example environments, contributor documentation, and source CI.

The underlying preview.10 implementation passed 48 mock gateway cases on each
architecture, 8 real text/cloud cases on each deployment, and 3 real H3 gateway
checks before and after an MLX service restart. See [validation](docs/validation.md).
Public example addresses and configuration are templates, not those tested hosts.

## Earlier private development

Earlier previews covered native chat, speech, multipart, image/audio/video and
WebSocket transports. Full historical deployment journals and Git history are not
published because they contain private infrastructure details. The initial public
commit contains the current implementation and attribution; historical tests are
not represented as newly rerun checks.
