# local-omni — docker

Pool selection:

```yaml
name: local-omni
pools:
- omni
```

Follow the [getting-started guide](../../../docs/getting-started.md) and, for
Compose, the [tools-container guide](../../../docs/docker.md). Configure the
example environment and load a matching router artifact before deployment.

```bash
python scripts/deploy.py deploy --runtime docker --environment docker \
  --example local-omni --overlay mock
```

For a new candidate add the explicit local release and candidate-test flags.
Remove `--overlay mock` only after configuring and verifying real engines,
credentials, weights and resource capacity. This scenario does not imply that
all declared engines fit concurrently on one GPU.
