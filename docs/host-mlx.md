# Native macOS MLX-Serve with a Kubernetes gateway

The hybrid profile keeps Envoy and SR in a Linux ARM64 VM while H3 runs natively
on macOS Metal. An independent NVIDIA host serves text. This avoids trying to
expose Metal as a Linux container GPU.

## Model and engine

`tools/host-mlx/versions.json` pins MLX-Serve **26.9.6** and the
`ddalcu/MiniMax-H3-FL2VA-MLX-Serve-4bit` model revision
`9c9273578ed5c2daf0b9a42eace823a6a49050ba`. The 15-file pack is approximately
41.1 GB. `model-manifest.json` records LFS SHA256 and Git blob SHA1 metadata.
Download and verify it with:

```bash
python tools/host-mlx/download.py --directory /opt/omni-runtime/host-mlx/models/MiniMax-H3-FL2VA-4bit
```

The default endpoint is Hugging Face. `--endpoint https://hf-mirror.com` is an
explicit alternative with the same pinned revision and hash checks. The code
repository does not redistribute weights. Review the model and engine licenses.

Download the engine archive named in `versions.json`, verify its SHA256, and
extract it under `/opt/omni-runtime/host-mlx/bin/`, preserving the sibling dynamic
libraries and Metal resources. Create a private `secrets/MODEL_API_KEY` file
beneath the same root and restrict it to mode 0600. Copy the identical model key
to project A's private environment credentials.

`serve.py` reads `MLX_HOME` (default `/opt/omni-runtime/host-mlx`) and `MLX_HOST`
(default `127.0.0.1`). Choose a reachable bind address for the gateway and suitable
network access controls. The launchd templates require editing paths and the
Colima user (`operator` is a placeholder) before installation.

## Gateway and network

Use the `hybrid` environment and its catalog. Configure the actual text-host
address, MLX address, cloud endpoint and private credentials. MLX's default loopback
bind can be reached through Lima's host gateway when that networking path is
configured; verify it from the VM before deploying.

The observed host used a 4-CPU, 6-GiB Colima VM with no host mounts. Native VZ shared
networking (`--network-address --network-preferred-route`) provided access to the
text host's LAN endpoint. VPN/overlay-network reachability from a VM is a separate
check from reachability on its host. Do not copy an untested route.

`containerd.toml` is an optional profile override for the verified K3s
`v1.36.4+k3s1` air-gap image set; import the matching ARM64 archive before selecting
its pause image. The launchd examples use RunAtLoad/KeepAlive. Stop a daemon with
`launchctl bootout` and restore it with `launchctl bootstrap`; merely killing a
KeepAlive process causes a restart.

## Native media response

Use `POST /v1/video/generations` with `model: local-only` or `auto` and the native
MLX fields. `stream: true` returns progress SSE ending in a `type: complete` event.
The payload contains base64 RGB8 frames and PCM16 audio, not an MP4 or an async job ID.
The caller decodes/muxes using returned frame count, FPS, dimensions and audio metadata.

The explicit real smoke test is:

```bash
python tests/integration/h3_real.py --environment hybrid \
  --catalog environments/hybrid/catalog.yaml --url http://YOUR_GATEWAY:30800
```

It generates a short real clip and saves raw media under `.state/media/`.
The recorded acceptance used 256×256, 22 frames at 24 FPS, four turbo steps and
32 kHz stereo PCM on a 48-GiB Apple Silicon host. It does not establish a general
minimum-memory requirement, long-video support, Ref2VA or concurrent throughput.
See the companion [native protocol document](https://github.com/omni-runtime/semantic-router-multimodal/blob/main/docs/native-mlx-video.md).
