# Getting started

## 1. Prepare the source tools

Use Python 3.12+, Git and a target-specific client (`kubectl`, or Docker with
Compose v2 and include/volume-subpath support). Install the locked dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

Rendering Kubernetes configuration requires no cluster or credentials:

```bash
python scripts/deploy.py render --runtime kubernetes --environment kubernetes \
  --example cloud-only --overlay mock --config-only
```

`--config-only` skips release/credential readiness checks. It is not a deployment
or an inference test. `--overlay mock` explicitly substitutes model backends;
SR and Envoy remain the real components when the rendered deployment is run.

## 2. Supply the router artifact

Build a native image using the companion project's
[build guide](https://github.com/omni-runtime/semantic-router-multimodal/blob/main/docs/building.md).
The checked-in contract's image references identify historical local OCI artifacts,
not public registry packages. Obtain/build an image, import it into your cluster's
container runtime or push it to a registry you control, and record its immutable
reference, platform and provenance in a local copy of `contracts/release.yaml`.

For a newly built artifact, set the local contract `status: candidate` and the
image `acceptance: built-unit-tested` only after its build/tests pass. Use
`--release contracts/release.local.yaml --candidate-test --overlay mock` for first
acceptance. After the gateway suite succeeds, record its report and change the
local contract to `status: preview`, `acceptance: tested-with-mock`. Use the same
`--release` path for later deployments. Do not copy the historical acceptance
label to an untested binary. `contracts/release.local.yaml` is ignored by Git.

## 3. Configure an environment

Edit or copy `environments/kubernetes/`. The directory name must match `name`.
Use the actual kubeconfig, context, namespace, node, model root and reachable
SSH-host field (used by test clients to determine the gateway address). The CLI
runs where invoked; it does not automatically log into that host.

Model catalogs specify provider model IDs, endpoint URLs, capabilities, context
and output budgets. The default cloud URL/model are placeholders. Configure your
own OpenAI-compatible endpoint and matching model ID. A model capability declaration
must reflect the actual engine/model, not a desired feature.

The managed engine example assumes NVIDIA support and pre-existing model weights.
The sample resource profile does not authorize concurrent text and speech engines
on one small GPU. Use `cloud-only` or explicit mock backends to start without GPU
resources. The `hybrid` environment registers external text and MLX video services.

## 4. Initialize private credentials

```bash
cp secrets/credentials.env.example /path/to/private/credentials.env
# Edit that private file and restrict its permissions; never commit it.
python scripts/credentials.py --runtime kubernetes --environment kubernetes \
  --source-env /path/to/private/credentials.env
```

The initializer writes private files under `secrets/<environment>/` and applies a
namespace-scoped Secret. It generates missing gateway/model tokens and reads the
cloud key from the explicit source. Values are not printed or put in subprocess
arguments. Use `GATEWAY_TOKEN` for clients, not the backend API keys.

## 5. Deploy and verify a candidate

After setting up the local candidate contract and importing its image:

```bash
python scripts/deploy.py deploy --runtime kubernetes --environment kubernetes \
  --example vllm-omni-cloud --overlay mock \
  --release contracts/release.local.yaml --candidate-test
python tests/integration/run.py --runtime kubernetes --environment kubernetes \
  --example vllm-omni-cloud --overlay mock
```

For MLX protocol fixtures, use `--environment hybrid --catalog environments/hybrid/catalog.yaml`
on both commands. This needs an ARM64 router artifact for the sample hybrid profile.
To use a different architecture, update the profile and corresponding artifact.

Once mock acceptance is recorded and the local contract promoted, deploy the
chosen example without `--overlay mock` and run `tests/integration/real.py` with
matching environment/example/catalog arguments. Real tests invoke your configured
models and may incur cloud API costs.

## Operations

```bash
python scripts/deploy.py status --runtime kubernetes --environment kubernetes
python scripts/deploy.py logs --runtime kubernetes --environment kubernetes --service router
python scripts/deploy.py stop --runtime kubernetes --environment kubernetes --service vllm
python scripts/deploy.py start --runtime kubernetes --environment kubernetes --service vllm
python scripts/deploy.py down --runtime kubernetes --environment kubernetes
```

Stop intent is persisted; repeated deploys do not silently restart a deliberately
stopped service. `down` removes project workloads but preserves persistent data.
External services are managed on their own hosts. See [Docker](docker.md) for the
tools-container workflow and [MLX](host-mlx.md) for native macOS lifecycle.
