# Cloud and local embedding routing

The embedding extension adds `POST /v1/embeddings` to native SR. Use a router
artifact whose release contract lists `embeddings`, `embedding`, and the actual
input capabilities. The previous preview.10 image does not contain this extension;
configuration validation intentionally refuses to send it embedding traffic.
The examples reference the validated preview.11 contract in
`contracts/release.yaml`. Both architectures have native gateway acceptance;
real Qwen 2B and Ark requests were verified through the ARM64 gateway.

[`examples/embeddings/stack.yaml`](../examples/embeddings/stack.yaml) is an explicit
mock example. It uses the same unified configuration and `auto`, `local-only`,
`cloud-only` entrypoints as other tasks. Real deployment requires actual engine
URLs, credential references, checkpoint IDs and measured limits in that file.
Cloud discovery must confirm an embedding model, rather than reuse a Chat model ID.

```yaml
models:
  - name: local/embedding
    backend: local-embedding
    provider_model_id: Qwen/Qwen3-VL-Embedding-2B
    api_format: embeddings
    capabilities: [text, embedding, image_input]
    context_window: 4096
    max_output_tokens: 0
    embedding:
      space: qwen3-vl-embedding-2b-bf16-v1
      dimensions: 2048
      min_dimensions: 64
      max_batch_size: 1
      image_token_estimate: 1024
```

`embedding.space` identifies weights/revision, processor, pooling, normalization
and relevant precision choices. Different checkpoints and quantized variants
should use separate identities until compatibility has been measured. Matching
vector lengths alone is insufficient. `dimensions` is the default/maximum output
size; omit `min_dimensions` for a backend that only supports its full size.
`context_window` applies to each batch item. `max_batch_size` is an engine limit,
not a promise that the largest batch and context fit concurrently in memory.

```json
{"model":"local-only","input":"一只蓝色的鸟","dimensions":1024,"encoding_format":"float"}
```

Cloud requests use `model: cloud-only`. When a selected recipe contains different
spaces, `auto` requires `embedding_space`; SR consumes this routing constraint
and sends only backend API fields upstream. A successful response includes
`x-vsr-embedding-space`. Persist the space and dimensions with an index and supply
them on future requests. There is no implicit fallback across vector spaces.

Text input accepts a string or an ordered string batch. Token-ID input requires
the additional `embedding_token_input` capability. Multimodal requests use the
vLLM `messages` extension with one user message and optional system instruction;
`input` and `messages` are mutually exclusive. Declare image/video capabilities
only when the actual engine implements that contract. Float and base64 vectors,
indices, usage and backend errors pass through without Chat conversion.

For Apple Silicon the intended topology is K8s Envoy + native SR and a separate,
authenticated host embedding engine. The host service must run the real embedding
checkpoint with its prescribed pooling/normalization. The gateway registers it
as `deployment: {mode: external}`. Installation and memory acceptance of that
engine are separate from router acceptance; the existing H3-specific `mlx` tooling
does not automatically install an embedding engine.

Run mock routing acceptance after deploying an embedding-capable candidate to an
isolated namespace:

```bash
python tests/integration/embedding_cases.py --config instances/embedding-test/stack.yaml
```

This checks real SR/Envoy routing, credentials, scope, order, output bytes and
rejections against test engines. It does not establish real-model quality or
cloud embedding availability. Real acceptance additionally needs finite vectors,
dimensions, normalization, repeatability, text/image retrieval, memory headroom
under existing H3 load, and regression checks of existing text/video routes.

## Qwen 2B and Volcengine Ark

Use [the Ark example](../examples/ark-embeddings/stack.yaml) for an authenticated
local Qwen3-VL-Embedding-2B engine and `doubao-embedding-vision-251215` in Ark.
The local runtime installation and limits are documented in
[the host engine guide](../tools/host-embedding/README.md).

The cloud backend uses `provider: volcengine-ark`,
`base_url: https://ark.cn-beijing.volces.com/api/v3`, and
`api_format: ark_embeddings`. Its key has a separate dotenv reference,
`ARK_EMBEDDING_API_KEY`; chat credentials remain independent.

SR accepts `POST /v1/embeddings` for either backend. For Ark it converts one text
or one multimodal `messages` sample to `/api/v3/embeddings/multimodal` and wraps
Ark's `data` object as a single indexed OpenAI-compatible `data` array. Vector
numbers/base64, provider identifiers, nested token usage and HTTP errors retain
the provider's values. Batch text is rejected, never fused into one vector.

Ark supports 1024 or 2048 dimensions. Declare
`allowed_dimensions: [1024, 2048]` along with `min_dimensions: 1024`;
1536 is rejected during selection. Optional system text in `messages` maps to
Ark's `instructions` field. This adapter rejects `user`, video, sparse embeddings
and multi-vector options rather than silently dropping them.

```bash
curl "$GATEWAY_URL/v1/embeddings" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"cloud-only","input":"天很蓝，海很深","dimensions":1024}'
```

For joint text/image input, use `messages` with one user message containing
`text` and `image_url` content blocks. Use `model: local-only` for the local
Qwen engine. `auto` requires `embedding_space` when both vector spaces are in
scope. Store the response's `x-vsr-embedding-space` alongside your vector index;
equal dimensions do not make Qwen and Doubao vectors interchangeable.

[Ark's official guide](https://docs.volcengine.com/docs/ark/vectorization?lang=zh)
explains query/corpus instruction conventions. Use consistent conventions for
each index; this gateway does not infer the application's retrieval task.
