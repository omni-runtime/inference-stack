# Protocol support and boundaries

The router release declares implemented wire formats; model cards declare which
tasks each backend can actually execute. Mock acceptance is not evidence that an
arbitrary engine supports a task.

| Family | Ingress / behavior |
| --- | --- |
| Chat | OpenAI-compatible JSON/SSE, ordered multimodal content and declared output modalities |
| Speech | Native speech task, WAV/PCM or engine-supported SSE |
| Images/audio generation | Native task parameters and same-wire responses |
| ASR/translation/image editing | Typed multipart parsing and bounded file/field preservation |
| Synchronous video | Native multipart dispatch and MP4 passthrough |
| Asynchronous video | Instance-bound create/query/content/delete with scoped Redis TTL records |
| MLX H3 video | `/v1/video/generations`, native JSON/progress SSE, RGB8/PCM16 payload |
| Realtime/speech stream | Native WebSocket handshake selection and same-connection frames |

Use `auto`, `local-only`, or `cloud-only`. Physical model names and client-supplied
internal routing headers do not bypass policy. Cross-wire conversion of native
media is not supported. Unsupported capability combinations fail before dispatch.

Async video requires a stable concrete backend identity and Redis; list operations
are not implemented. WebSocket frames are not inspected or routed across models
mid-session. H3 uses a synchronous generation call even when progress is streamed;
it does not implement the async video job contract. Caller applications own
multi-step workflows and media muxing.

See [validation](validation.md) for the current positive-test scope and
[the companion protocol examples](https://github.com/omni-runtime/semantic-router-multimodal/tree/main/examples).
