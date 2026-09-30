# 部署项目设计

inference-stack 是离线运行的薄部署 CLI，管理 SR、Envoy、推理服务、存储和凭据引用。
所有业务判断来自 SR 原生配置，Python 不进入请求链路。

```text
Kubernetes: 客户端 → Envoy Service → 后端 ClusterIP Service
                        ↕ ExtProc
                       SR Service

Compose:    客户端 → Envoy 容器 → 后端容器/云端
                        ↕ ExtProc（服务 DNS）
                       SR 容器
```

两种运行环境、七种模型组合共用原生 SR recipes/model cards，只替换部署、网络、
存储及凭据引用。公共 K8s 资源由 Kustomize 复用，Compose 使用原生合并文件；
不转换任意 K8s YAML 为 Compose。每个模型池允许多个模型或副本。

check/render/deploy/start/stop/status/logs/test/down 都显式绑定环境、context 和
namespace/Compose project。stop 是操作员明确停止，部署不会暗中重新启动已停止
模型；start 才解除停止记录。切换组合列出新增/移除资源，清理仅限已登记的本项目
资源。down 保留权重、凭据、持久卷；不删除集群、Docker 或运行全局 prune。

docker 项目 CLI/依赖/测试/报告都在工具容器；源码通过 Docker cp/构建上下文传入
named volumes。唯一宿主机数据挂载是现有模型权重且必须只读。业务容器不得获得
Docker socket、privileged、host PID 或宿主机写挂载。宿主机只复用配套 Compose
5.1.4 管理客户端。工具容器通过 Docker 管理通道执行离线操作；通道不得新增宿主数据挂载，不运行在线控制器。

当前实施窗口：Asia/Shanghai 2026-09-29 08:00 后才可执行 Docker 验证。
此前只做源码、文档、非 Docker 单元检查与 Kubernetes 验证。
