# inference-stack

[English](README.md) · [快速开始](docs/getting-started.md) · [验证范围](docs/validation.md)

**面向 Kubernetes、Docker Compose 和外部模型主机的能力感知推理网关部署工具。**

本仓库负责配置和运维；配套的
[semantic-router-multimodal](https://github.com/omni-runtime/semantic-router-multimodal)
负责原生 Semantic Router 补丁、构建与发布契约。Envoy 转发请求，SR 根据任务能力、
recipe 和模型范围选择后端。Python 仅作为离线操作工具，不代理推理或创建业务工作流。

## 主要能力

- K8s 与 Compose 共用模型目录和原生 SR 配置。
- 七种显式的本地文本、本地多模态与云端组合。
- `auto`、`local-only`、`cloud-only` 入口，认证、能力过滤和范围隔离。
- vLLM/vLLM-Omni 托管服务，以及其他主机上的外部服务。
- K8s 网关＋NVIDIA 文本主机＋macOS Metal H3 的混合示例。
- 显式部署、启动、停止、状态、日志、测试与卸载；停止意图持久保存。

## 本地快速开始

需要 Python 3.12+。以下步骤只渲染配置，不启动模型，也不需要集群：

```bash
git clone https://github.com/omni-runtime/inference-stack.git
cd inference-stack
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python scripts/deploy.py render --config examples/vllm-omni-cloud/stack.yaml --config-only
python -m unittest discover -s tests/unit -v
```

生成结果在 `generated/sample-vllm-omni-cloud/stack/`。
真实部署前，按[统一配置指南](docs/configuration.zh-CN.md)建立
`instances/<name>/stack.yaml` 和 `secrets.env`，集中填写主机、模型、资源和凭据引用，
并准备 kubeconfig 及配套 Router 镜像。

**当前是 preview.10 源码预览版。** 发布契约中的镜像摘要标识已实测的本地 OCI 产物，
尚未发布到公共镜像仓库，不能假定可以直接拉取。仓库不包含模型权重或真实凭证。

## 统一配置入口

日常修改一份 `stack.yaml`，私有密钥放在 `secrets.env`，运行清单统一生成。
[混合部署示例](examples/hybrid/stack.yaml)覆盖 K8s 网关、远端文本和宿主 MLX。
所有操作使用同一个 `--config`；check/render/plan 为离线检查与预览，
部署和真实模型验证仍需显式执行。旧参数保留迁移兼容。
详见[配置与迁移指南](docs/configuration.zh-CN.md)。

## 配置与验证

`kubernetes` 是 Linux 集群示例，`docker` 是工具容器内操作的 Compose 示例，
`hybrid` 是外部文本＋MLX-Serve 视频示例。`example.com`、`example.test` 和
`192.0.2.0/24` 都是示例地址，部署时需要替换。

底层实现已通过 AMD64、原生 ARM64 各 48 项 mock 网关检查、两端真实文本/云端检查，
以及 H3 服务重启前后的真实短视频检查。[脱敏验证摘要](docs/validation.md)保留了范围、
结果和原始报告摘要，不将历史实验包装成本次 CI 结果或所有硬件通用的保证。

H3 返回原生 RGB8/PCM16 JSON 或进度 SSE，需要客户端封装成 MP4；视频长时、所有分辨率、
Ref2VA 和并发压力未在这次验收范围内。详见[协议说明](docs/protocols.md)和
[MLX 运维指南](docs/host-mlx.md)。

## 参与和许可

请阅读[贡献指南](CONTRIBUTING.md)、[社区规范](CODE_OF_CONDUCT.md)和
[安全报告方式](SECURITY.md)。代码使用 [Apache-2.0](LICENSE)，上游归属见 [NOTICE](NOTICE)。
模型、引擎与基础镜像保留各自许可证。本项目是独立集成项目，不是 vLLM 官方发布。
