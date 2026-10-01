# Unified deployment configuration

Project A accepts one `stack.yaml` per deployment. Project B supplies the native
router and its release contract; it does not maintain a second deployment
configuration. The complete shape is checked against
[`stack.schema.json`](../schemas/stack.schema.json).

## Create an instance

```bash
mkdir -p instances/lab
cp examples/hybrid/stack.yaml instances/lab/stack.yaml
cp examples/hybrid/secrets.env.example instances/lab/secrets.env
chmod 600 instances/lab/secrets.env
```

Edit `instances/lab/stack.yaml`, including `name: lab`, and fill the secret file.
Put the kubeconfig next to it. The hybrid example uses **real external services**;
its addresses are placeholders. Set `mode: mock` explicitly for fixture backends.
The seven pool examples and the Docker example default to mock mode.

| Section | Operator-owned inputs |
| --- | --- |
| `hosts` | SSH identity and absolute remote project directory; optional platform description |
| `gateway` | Runtime, platform, project, host, Kubernetes/Compose settings; optional test-client URL |
| `resources` | Gateway limits and verified managed-engine capacity |
| `backends` | Pool, provider, reachable URL, credential name, lifecycle mode and engine launch configuration |
| `models` | Backend reference, provider model ID, protocol, capabilities and budgets |
| `routing.enabled_models` | Explicit model list; the usual auto/local-only/cloud-only scope rules still apply |
| `artifacts` | Router release contract and component image lock file |
| `secrets.file` | Literal dotenv source, never embedded key values |

A backend is defined once even when several model/protocol entries use it.
`deployment.mode: managed` belongs to the selected gateway target; its `command`
is an argument array and `config_files` embeds engine YAML objects in this file.
`external` registers an independently operated service and accepts no managed
launch options. It neither starts/stops the remote service nor claims its GPU.
Cloud services use external mode too. `mlx` is optional explicit host tooling
configuration, separate from gateway-managed lifecycle.

`gateway.url` is the address used by test clients. A backend's `base_url` must be
reachable from the gateway. `hosts.*.ssh` describes management access; the CLI
does not infer network reachability from SSH or automatically log in.

## Paths, defaults and validation

Local file references (release, versions, kubeconfig, secrets, MLX locks) resolve
relative to the selected stack file, independent of the shell working directory.
Remote host roots, model paths and MLX home are host paths and must be configured
for that host. There is no implicit environment-variable expansion or overlay merge.

Explicit values win over documented implementation defaults. State tracks resource
inventory and operator stop intent; it cannot override mode, model selection,
artifact paths or endpoint URLs in a selected stack file. Do not combine `--config`
with legacy `--environment`, `--runtime`, `--example`, `--catalog`, `--release` or
`--overlay`. `--service`, `--config-only` and `--candidate-test` remain action options.

Unknown fields, duplicate YAML keys, duplicate model IDs/listener ports, dangling
references, embedded URL credentials and invalid managed/external combinations
fail before runtime calls. Engine-specific inline YAML remains engine-owned; this
schema checks its container and filename, not every third-party engine setting.
Release checks verify protocol/capability declarations against the selected artifact.

```bash
python scripts/deploy.py check --config instances/lab/stack.yaml --config-only
python scripts/deploy.py render --config instances/lab/stack.yaml --config-only
python scripts/deploy.py plan --config instances/lab/stack.yaml
```

These unified commands are offline. `check` without `--config-only` also checks
release eligibility, resource declarations and required source credentials; it
still does not contact the cluster. `render` without that flag checks release and
resources. Neither is evidence of image availability or successful inference.

`generated/<name>/stack/` contains native router/Envoy configuration and K8s/Compose
files. It also records the non-secret input document as `resolved.yaml` (paths
retain their source-file semantics; this is an inspection record, not a portable
replacement config). Outputs are replaced only after a complete generation succeeds.
Do not edit them: change the stack file and render again.

`plan` renders in a temporary directory and compares against the last **successful
local deployment**. It lists desired creates/removals and conservative possible
restarts, preserved stops and separately operated host configuration changes.
It does not inspect live drift. Without a baseline it says so; it cannot establish
that the target is empty or promise an exact server-side diff. It never starts or
restarts external services and never includes secret values.

## Credentials and lifecycle

Each backend references an environment name ending in `_API_KEY`. Different
backends may have different keys. Clients use `GATEWAY_TOKEN`. Dotenv values are
literal; quotes may surround a value, but shell commands and variable expansion
are not evaluated. Supply non-empty actual values; placeholders are rejected.

```bash
python scripts/credentials.py --config instances/lab/stack.yaml
python scripts/deploy.py deploy --config instances/lab/stack.yaml
python scripts/deploy.py status --config instances/lab/stack.yaml
python scripts/deploy.py logs --config instances/lab/stack.yaml --service router
python scripts/deploy.py test --config instances/lab/stack.yaml
```

Credential initialization stages mode-0600 files in `secrets/<name>/` and, for
Kubernetes, applies the namespace Secret. This is a mutating command. Docker
operations still run inside the authorized tools container with its existing
private credential/config mounts. Rerun initialization after editing the secret
source; deploy rejects missing/stale staged values. Runtime secret drift outside
these tools is not detected by the offline check.

`start`, `stop` and `down` accept the same `--config` argument. Stop intent survives
redeployment. Different instances need distinct namespaces or Compose projects;
use a new instance name when changing an already recorded deployment target.
`test` chooses mock or real probes from `mode`; real probes call the configured
backends. H3 media acceptance remains explicit:

```bash
python tests/integration/h3_real.py --config instances/lab/stack.yaml
```

## MLX host settings

The hybrid sample declares `backends.mlx-h3.mlx` with host home, listen address,
port, concurrency, timeout, model directory, engine lock and model manifest.
Default listener is `127.0.0.1`, port 11234, concurrency 1, timeout 3600 seconds.
Set a reachable host interface for a gateway in another VM or machine. H3 revision
comes from the manifest; conflicting explicit revisions are rejected.

On the declared MLX host, with the project and Python dependencies installed:

```bash
python tools/host-mlx/download.py --config instances/lab/stack.yaml --backend mlx-h3
python tools/host-mlx/serve.py --config instances/lab/stack.yaml --backend mlx-h3
```

These are explicit download/start operations. The matching backend key must be
installed at `<mlx.home>/secrets/<api_key_env>` with restricted permissions.
Rendering also creates `host-mlx/<backend>/settings.json` and `service.plist`.
For launchd, install settings as `<mlx.home>/settings.json`, ensure the engine,
project script and log directory exist on that host, and explicitly install or
reload the generated plist. `serve.py --settings ...` needs only the Python
standard library. Gateway deployment does not install host files or restart MLX.
Existing no-argument MLX launchers retain their legacy environment-variable behavior.

## Migrate existing inputs and consume project B

```bash
python scripts/configure.py migrate \
  --environment hybrid --example vllm-omni-cloud \
  --catalog environments/hybrid/catalog.yaml --mode real \
  --name lab --output instances/lab/stack.yaml
```

Migration consolidates existing environment/catalog/scenario inputs and embeds
engine config files. Historical model `validation` annotations remain in the
untouched source catalog; they are not copied into desired configuration.
Migration never overwrites the output or copies credentials. An
optional `--source-root /path/to/old/inference-stack` imports another checkout.
Relative artifact references continue pointing at their original files; import
needed contracts into this checkout before retiring the old directory. Existing
legacy arguments remain supported for legacy instances. Once an instance is
deployed with `--config`, subsequent operations must use that same configuration
entrypoint; legacy commands will reject the migrated state explicitly. MLX host setup is outside the
legacy catalog; add its `mlx` block using the hybrid example when migrating it.

```bash
python scripts/configure.py import-release \
  --source ../semantic-router-multimodal/release.yaml \
  --output locks/router.local.yaml
```

Set `artifacts.release: ../../locks/router.local.yaml` in `instances/lab/stack.yaml`.
The command validates structural artifact identifiers and copies exact bytes,
including acceptance labels; it does not create new acceptance evidence. A new
candidate still requires explicit `mode: mock` and `--candidate-test`, followed
by recorded gateway acceptance. The checked-in preview images remain historical
local OCI artifacts, not published registry downloads.

Embedding models use `api_format: embeddings` and a model-level `embedding` declaration. See [embedding configuration and acceptance](embeddings.md).

Ark uses `api_format: ark_embeddings`, discrete `embedding.allowed_dimensions`,
and a separate credential reference. The optional MLX embedding backend uses
`mlx.engine: mlx-embeddings`; its Python executable, model lock, input/pixel limits
and memory cap are declared in the same backend block. Rendering produces host
settings and a launchd plist; only an explicit host installation restarts the engine.
