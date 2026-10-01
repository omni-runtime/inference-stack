# 统一配置

日常部署只维护项目 A 中的一份 `stack.yaml` 和旁边的 `secrets.env`。
项目 B 继续提供原生路由实现与发布契约；A 读取契约生成运行配置。
完整字段见 [JSON Schema](../schemas/stack.schema.json)，详细行为见
[英文说明](configuration.md)。

## 建立自己的环境

```bash
mkdir -p instances/lab
cp examples/hybrid/stack.yaml instances/lab/stack.yaml
cp examples/hybrid/secrets.env.example instances/lab/secrets.env
chmod 600 instances/lab/secrets.env
```

修改 `stack.yaml` 的 `name: lab`，填写自己的主机、端口、路径和模型信息，
并把 kubeconfig 放在同目录。`instances/` 内实际环境文件不会提交 Git。
不同环境还需要不同的 K8s namespace 或 Compose project，仅改 name 不会隔离运行资源。

| 配置块 | 在这里改什么 |
| --- | --- |
| `hosts` | SSH 目标、远端项目目录 |
| `gateway` | K8s/Compose、平台、namespace、节点、端口、测试客户端 URL |
| `resources` | 网关和受管模型资源、GPU 并发约束 |
| `backends` | vLLM、MLX、云端地址，密钥名称，启动参数，管理方式 |
| `models` | 模型 ID、能力、上下文与输出预算、所属 backend |
| `routing.enabled_models` | 此环境实际启用的模型 |
| `artifacts` | B 发布契约与组件版本锁的路径 |
| `secrets.file` | 私有密钥文件位置 |

SSH 管理地址与网关能访问的推理地址分别配置。同一后端的 URL、认证和启动设置只定义一次。
`external` 后端由其所在主机独立运行，网关不会启动或停止它；`managed` 后端由选定的
K8s/Compose 目标管理。模型专用 YAML 可直接内嵌在 `deployment.config_files` 中。

`mode: real` 使用真实后端；`mode: mock` 明确启用模拟后端。混合示例默认为 real，
七种组合示例及 Docker 示例默认为 mock。切换模式必须改配置，旧部署状态不会替你覆盖。

## 常用命令

```bash
python scripts/deploy.py check --config instances/lab/stack.yaml --config-only
python scripts/deploy.py render --config instances/lab/stack.yaml --config-only
python scripts/deploy.py plan --config instances/lab/stack.yaml
```

以上命令均为离线操作。`--config-only` 用于只检查/生成配置；去掉后还会检查发布资格、
资源声明，check 也会检查源密钥是否齐全，但仍不能证明镜像可拉取、集群可用或模型推理成功。
生成文件位于 `generated/<name>/stack/`，不要手工修改。

`plan` 对比上一次成功部署保存的本地快照，显示新增、删除、可能重启、保持停止的服务，
以及需单独处理的宿主配置变更。它不会查询线上漂移；没有快照时会明确提示。
密钥不会进入预览或生成的运行清单。

配置文件中的本地文件路径相对于 `stack.yaml` 所在目录解析，远端根目录和模型路径是
对应主机上的路径。不做隐式环境变量展开，不允许 `--config` 与旧的 environment/runtime/
example/catalog/release/overlay 参数混用。

填写 `secrets.env` 后执行：

```bash
python scripts/credentials.py --config instances/lab/stack.yaml
python scripts/deploy.py deploy --config instances/lab/stack.yaml
python scripts/deploy.py status --config instances/lab/stack.yaml
python scripts/deploy.py logs --config instances/lab/stack.yaml --service router
python scripts/deploy.py test --config instances/lab/stack.yaml
```

凭据初始化会写入权限为 0600 的派生密钥文件，K8s 模式还会更新 Secret。
更换密钥后需要重新执行初始化，deploy 会拒绝使用与源文件不一致的派生凭据。
每个后端可以引用独立的 `*_API_KEY`，网关客户端使用 `GATEWAY_TOKEN`。
`secrets.env` 是字面值文件，不执行其中的命令，也不展开变量。

start/stop/down 使用同一个 `--config`；人为停止状态会保留。test 会根据 mode
选择模拟或真实检查，real 会调用实际后端。Docker 的实际操作仍须在工具容器内进行。

## MLX 宿主服务

`backends.mlx-h3.mlx` 集中配置目录、监听地址、端口、并发和超时。
默认监听 `127.0.0.1`；跨虚拟机访问时需要改成实际可达的接口。
权重版本来自 manifest，无需重复填写版本哈希。

在 MLX 所在主机执行：

```bash
python tools/host-mlx/download.py --config instances/lab/stack.yaml --backend mlx-h3
python tools/host-mlx/serve.py --config instances/lab/stack.yaml --backend mlx-h3
```

对应后端的密钥需安装在 `<mlx.home>/secrets/<api_key_env>`。
render 也会生成 `host-mlx/<backend>/settings.json` 与 `service.plist`，供显式安装到宿主机。
网关 deploy 不会自动复制这些文件或重启 MLX。已有无参数启动方式继续兼容。

## 迁移与版本升级

```bash
python scripts/configure.py migrate \
  --environment hybrid --example vllm-omni-cloud \
  --catalog environments/hybrid/catalog.yaml --mode real \
  --name lab --output instances/lab/stack.yaml
```

迁移不覆盖已有文件，不复制密钥。旧模型条目中的历史 validation 注释保留在原目录，
不作为期望配置导入。可用 `--source-root` 指向旧代码目录。
旧目录中独立维护的 MLX 宿主设置需要参照混合示例补入 mlx 配置块。
迁移后的契约引用仍指向来源文件，删除旧目录前应导入到新目录。

```bash
python scripts/configure.py import-release \
  --source ../semantic-router-multimodal/release.yaml \
  --output locks/router.local.yaml
```

然后设置 `artifacts.release: ../../locks/router.local.yaml`。
导入只校验并复制 B 的原始发布契约，不产生新的验收结论，不会发布镜像。
新构建的 candidate 仍须在 mock 模式下用 `--candidate-test` 完成显式网关验收。
旧参数入口目前保留，用于迁移兼容。

Embedding models use `api_format: embeddings` and a model-level `embedding` declaration. See [embedding configuration and acceptance](embeddings.md).

方舟多模态 embedding 使用 `api_format: ark_embeddings`、离散的
`embedding.allowed_dimensions` 和独立密钥引用。本地 embedding 后端设置
`mlx.engine: mlx-embeddings`；Python 路径、模型锁、输入/图片上限和内存限制
仍写在同一份 `stack.yaml` 中，渲染生成宿主配置和 launchd 服务文件。
