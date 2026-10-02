# Mixed-architecture Kubernetes cluster

Copy `stack.yaml` and `secrets.env.example` into your private instance directory,
then set reachable control-plane/worker addresses, interface names and storage.
The documentation-only IP addresses must be replaced. Run the control plane in
a Linux VM when the gateway host is macOS; native MLX remains outside that VM.

Follow [the cluster deployment guide](../../docs/kubernetes-cluster.md). The
configuration uses one cluster and manages the text backend on its GPU worker.
Cloud and macOS Metal services remain external. Model weights, device drivers
and container images must be prepared separately.
