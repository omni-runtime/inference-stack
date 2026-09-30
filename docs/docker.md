# Docker Compose workflow

The example `docker` profile runs operator commands inside a tools container at
`/workspace/inference-stack`. Serving containers receive only project named
volumes and an explicitly allowed read-only model directory; they do not receive
Docker management credentials or the Docker socket.

Build the tools image from the repository root:

```bash
docker build -f tools/container/Dockerfile -t inference-stack-tools:local .
```

Create the named volumes listed under `docker.volumes` in the environment file.
Populate the source volume with this checkout and mount it at
`/workspace/inference-stack`. Mount the credentials volume at
`/workspace/inference-stack/secrets/docker`, and the configuration volume at
`/runtime-config`. Use a reports volume for generated evidence. These are
operator-controlled setup steps; the templates do not create host directories.

Give the tools container a Docker client connection using a dedicated context or
an explicitly authorized Docker socket mount. A socket grants broad daemon access;
keep it confined to the operator tools container. The optional
`tools/container/connect-context.sh` accepts a mutual-TLS endpoint and context name
when client certificates are mounted under `/run/docker-client`.

Configure `docker.context`, project/volume names and the exact existing read-only
model source. Check the platform against the real daemon. The sample is ARM64 and
has no verified GPU devices; it is suitable for explicit mock/cloud-only validation
once the router artifact has been loaded. Local engines require a verified GPU
configuration and compatible model weights; emulation is not acceptance.

Inside the tools container:

```bash
python scripts/credentials.py --runtime docker --environment docker \
  --source-env /run/private/credentials.env
python scripts/deploy.py check --runtime docker --environment docker \
  --example cloud-only --overlay mock
python scripts/deploy.py deploy --runtime docker --environment docker \
  --example cloud-only --overlay mock
python scripts/deploy.py test --runtime docker --environment docker
```

For a newly built candidate use the explicit local release and candidate flags
from [getting started](getting-started.md). Deployment checks daemon architecture,
port ownership, serving mount boundaries, read-only credentials/configuration and
resource capacity before modifying the Compose project. Only Envoy publishes a
business port; mock/model backends remain on the project network.
