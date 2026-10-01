# Qwen3-VL embedding engine on Apple Silicon

This optional host process performs model inference. Native SR owns routing,
provider credentials and Ark protocol translation; the host process does not
forward requests to another inference service.

`versions.json` pins the Qwen/Qwen3-VL-Embedding-2B checkpoint and MLX Embeddings
source revision. `requirements.lock` records the tested Python environment.
The original BF16 weights occupy 4.26 GB. No quantized replacement is selected.

## Install and configure

Create a dedicated virtual environment on the model host and install
`requirements.lock`. Download and verify the pinned model using Project A's
existing downloader with `--config /path/to/stack.yaml --backend local-embedding`.
Use `--endpoint https://hf-mirror.com` when the host cannot reach Hugging Face.

The backend's `mlx` section in `stack.yaml` declares `engine: mlx-embeddings`,
`home`, `python`, `listen`, `port`, `model_directory`, `model_manifest`, and
`engine_lock`. The manifest and lock paths point to this directory's JSON files.
Model identity, dimensions and batch size come from the backend's single model
entry. `scripts/deploy.py render --config ...` generates host settings and a
launchd plist alongside gateway manifests. Install the generated host settings
and plist explicitly; gateway deployment does not restart host engines.

Run `serve.py --config /path/to/stack.yaml --backend local-embedding`, or use
`--settings /path/to/generated/settings.json`. The generated launchd service uses
the latter. Put the configured backend credential in `home/secrets/<api_key_env>`
with mode 0600. Keys never appear in process arguments.

## Runtime contract

- Authenticated `GET /health` and `POST /v1/embeddings`.
- One text sample or one `messages` sample with text and at most one image.
  HTTPS and inline base64 images are supported; local files and private image
  URLs are rejected. Image detail must be omitted or `auto`.
- Default 2048 dimensions; 64–2048 dimensions with truncation followed by FP32
  normalization. Float and little-endian float32 base64 output are supported.
- Last non-padding token pooling uses the pinned Qwen embedding implementation.
  Independent requests clear the backbone's position cache to avoid cross-request
  state and the upstream shorter-sequence indexing bug.
- Default limits: concurrency 1, one sample, 4096 input tokens, 262144 image
  pixels after resizing, 8 GiB MLX allocation cap, 512 MiB MLX cache. Input over
  the token limit is rejected; truncated vectors are never returned.
- Declare `embedding.image_token_estimate: 1024` for this bounded image processor.
  This is an admission estimate, not authoritative tokenizer usage. The engine
  returns the actual token count.
- Video and token-ID input are not enabled by this host engine. The checkpoint's
  broader theoretical capabilities must not be advertised as deployed features.

The local and cloud embedding spaces are separate. Keep preprocessing, model
revision, dimensions and instruction conventions stable when creating and querying
an index. Changing these requires revalidation and usually reindexing.

The short coexistence test uses H3 at 256×256, 22 frames, 4 steps while issuing
embedding requests. It does not establish memory bounds for arbitrary H3 videos
or unbounded concurrent workloads.

References: [Qwen model card](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B),
[MLX Embeddings](https://github.com/Blaizzy/mlx-embeddings).

## License

The standalone host server is GPL-3.0-only; see [LICENSE](LICENSE). It imports
the separately installed GPL-3.0 MLX Embeddings library. Project A orchestration
and Project B native SR retain their existing licenses. Qwen weights are
downloaded separately under the model publisher's Apache-2.0 terms.
