# vllm-cloud — kubernetes

Pool selection:

```yaml
name: vllm-cloud
pools:
- vllm
- cloud
```

Follow the [getting-started guide](../../../docs/getting-started.md) and, for
Compose, the [tools-container guide](../../../docs/docker.md). Configure the
example environment and load a matching router artifact before deployment.

```bash
python scripts/deploy.py deploy --runtime kubernetes --environment kubernetes \
  --example vllm-cloud --overlay mock
```

For a new candidate add the explicit local release and candidate-test flags.
Remove `--overlay mock` only after configuring and verifying real engines,
credentials, weights and resource capacity. This scenario does not imply that
all declared engines fit concurrently on one GPU.
