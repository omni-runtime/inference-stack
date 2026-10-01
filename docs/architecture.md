# Architecture

`inference-stack` owns deployment templates, model catalogs, operator commands,
credentials integration, lifecycle operations and gateway tests.
[`semantic-router-multimodal`](https://github.com/omni-runtime/semantic-router-multimodal)
owns the locked upstream source, native protocol patch, builds and release contract.

For each inference request, Envoy calls native SR through ExtProc. SR parses the
protocol into a task, checks recipe/model capabilities and budgets, runs native
selection, binds the provider and encodes dispatch. Envoy sends the request to
the selected backend. Native media streams pass through without a full buffered
response codec. Python is not on this path.

`model` identifies a permitted entrypoint (`auto`, `local-only`, `cloud-only`).
`provider_model_id` identifies the actual backend model. `api_format` declares the
wire protocol, and `base_url` declares its network binding. These are separate
concepts; an engine name does not imply support for every media task.

## Ownership boundaries

- Managed backends are deployed and stopped explicitly by project A.
- `deployment.mode: external` registers a reachable backend and excludes its
  lifecycle and GPU capacity from the local gateway's managed resources.
- The router does not start engines or free occupied GPUs.
- Caller applications orchestrate multi-step inference.
- Async job continuations use the concrete instance selected at creation, scoped
  Redis bindings and TTL; they do not run selection again.
- WebSocket selection occurs at the native handshake, not for every frame.

See the detailed [request design](design.md) and [protocol limits](protocols.md).

## Configuration ownership

`stack.yaml` is project A's single deployment input. Its schema and loader validate
references before generating router, Envoy, Kubernetes/Compose and explicit MLX
host settings. Credentials remain in a separately referenced dotenv file. Build
locks and project B's release contract are immutable artifact metadata, not a
second set of host/model settings. Inventory records stop intent without overriding
the selected input. See [configuration](configuration.md).
