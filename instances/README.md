# Private deployment instances

Copy an example directory into `instances/<name>/`, change `name` in its
`stack.yaml`, and supply `secrets.env` and a kubeconfig there. Instance contents
are ignored by Git. Each instance needs its own Kubernetes namespace or Compose
project; changing the local name alone does not isolate runtime resources.

See [unified configuration](../docs/configuration.md) and the
[Chinese guide](../docs/configuration.zh-CN.md).
