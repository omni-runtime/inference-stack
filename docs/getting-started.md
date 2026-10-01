# Getting started

## Prepare the tools and choose one stack file

Use Python 3.12+, Git and a target-specific client (`kubectl`, or Docker with
Compose include/volume-subpath support). Install the locked dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python scripts/deploy.py render --config examples/cloud-only/stack.yaml --config-only
```

This needs no cluster or credentials. The example explicitly uses mock backends.
It is not a deployment or inference test. SR/Envoy remain the real components
when you actually run the rendered deployment.

Create a private instance from a [unified example](configuration.md#create-an-instance).
Use `examples/hybrid/stack.yaml` for external text plus macOS MLX video, or a pool
example for managed NVIDIA engines. Configure hosts, reachable backend URLs,
provider model IDs, actual capabilities, resource limits and kubeconfig in that
one file. Select `mode: mock` for initial gateway acceptance. The CLI executes
where invoked; a configured SSH identity does not cause an automatic remote login.

## Supply a router artifact

Build an image using project B's
[build guide](https://github.com/omni-runtime/semantic-router-multimodal/blob/main/docs/building.md).
The checked-in digest references identify historical local OCI artifacts, not
public registry packages. Build/import an image into the target runtime or publish
it to your own registry and produce the corresponding contract in project B.

```bash
python scripts/configure.py import-release \
  --source ../semantic-router-multimodal/release.yaml \
  --output locks/router.local.yaml
```

Point `artifacts.release` in the instance at `../../locks/router.local.yaml`.
Import preserves existing evidence claims; it does not verify the runtime or make
an untested image accepted. A newly built artifact uses `status: candidate` and
`acceptance: built-unit-tested` only after build tests pass. Its first gateway
acceptance requires explicit `mode: mock` plus `--candidate-test`.

## Configure credentials, preview and deploy

Fill the instance's referenced `secrets.env`, using `GATEWAY_TOKEN` and the named
backend keys. Never commit it. Initialize runtime credentials explicitly:

```bash
python scripts/credentials.py --config instances/lab/stack.yaml
python scripts/deploy.py check --config instances/lab/stack.yaml --candidate-test
python scripts/deploy.py plan --config instances/lab/stack.yaml
python scripts/deploy.py deploy --config instances/lab/stack.yaml --candidate-test
python scripts/deploy.py test --config instances/lab/stack.yaml
```

Omit `--candidate-test` when the selected contract already carries appropriate
acceptance. The initializer writes private mode-0600 files and, in Kubernetes,
applies a namespace Secret. Offline checks do not establish cluster reachability.
The hybrid example selects ARM64; use a contract/image matching the actual platform.

After mock gateway acceptance, record the report and promote the local contract
accordingly. Select `mode: real` in the same stack file for real backends, reinitialize
credentials as needed, deploy without `--candidate-test`, and run explicit real tests.
Those tests invoke configured models and may incur provider charges. H3 generation
is a separate [explicit media test](configuration.md#credentials-and-lifecycle).

## Operate the same instance

```bash
python scripts/deploy.py status --config instances/lab/stack.yaml
python scripts/deploy.py logs --config instances/lab/stack.yaml --service router
python scripts/deploy.py stop --config instances/lab/stack.yaml --service vllm
python scripts/deploy.py start --config instances/lab/stack.yaml --service vllm
python scripts/deploy.py down --config instances/lab/stack.yaml
```

Only managed services can be started/stopped this way. Stop intent is preserved;
`down` removes project workloads while retaining persistent data. External models
are operated separately on their hosts. See [Docker](docker.md), [MLX](host-mlx.md)
and [configuration/migration](configuration.md) for details and legacy compatibility.
