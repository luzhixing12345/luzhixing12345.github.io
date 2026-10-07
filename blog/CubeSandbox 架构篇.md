# CubeSandbox 架构篇

CubeSandbox 是腾讯云在 2026 年开源的 AI Agent 沙箱，仓库是 [TencentCloud/CubeSandbox](https://github.com/TencentCloud/CubeSandbox)，协议 Apache-2.0。每台沙箱是一台带独立内核的 KVM 微虚拟机，对外兼容 [E2B](https://e2b.dev/) 的 SDK，官方给出的单并发创建时间在 60 毫秒以内。

这是四篇系列的第一篇，讲整体设计和组件分工。另外三篇分别拆开讲[存储](/blog/cubesandbox-storage/)、[网络](/blog/cubesandbox-network/)和[虚拟化](/blog/cubesandbox-virtualization/)。文中的描述以仓库源码为准，和官方文档不一致的地方会指出来。

## 先把难题说清楚

Agent 用沙箱的方式和传统云主机很不一样。一次对话里可能要开好几台，开完马上就用，用几秒或几分钟就扔；同一台机器上要挤几千台；里面跑的是模型生成的代码，不能信任。

这几条放在一起很难同时满足。容器启动快、密度高，但和宿主机共享内核，一个内核漏洞就能逃逸。虚拟机隔离够硬，可冷启动一台要走固件、内核、init、服务初始化，少说几百毫秒，还要为每台预留整份内存。

CubeSandbox 的回答可以压成一句话：**把冷启动的成本挪到模板阶段，每台节点只付一次；运行时的创建只做本机克隆和快照恢复。**模板构建时，节点真的冷启动一台虚拟机，等服务就绪后把内存和磁盘冻结成快照。之后每次创建，磁盘用 XFS 的 reflink 克隆，内存把快照文件直接映射进来，几乎不搬数据。

这个思路决定了后面的许多设计：调度器必须知道哪台节点上已经有这份模板；存储必须建在支持 reflink 的本地 XFS 上；暂停、克隆、回滚都可以归结成“拍快照”和“从快照创建”的组合。

## 组件全景

```demo
demos/cube-arch.html
```

组件分成两层。控制面负责接请求、调度、记账，计算节点负责真正把虚拟机跑起来。

| 组件 | 语言 | 位置 | 做什么 |
| --- | --- | --- | --- |
| CubeAPI | Rust | 控制面 | 兼容 E2B 的 REST 网关，鉴权、限流，翻译成内部 HTTP 调用 |
| CubeMaster | Go | 控制面 | 调度、编排创建/暂停/恢复/销毁、模板状态 |
| CubeOps | Go | 控制面 | 节点清单的权威来源、运维接口、WebUI 后端 |
| TemplateCenter | Go | 控制面 | 把 OCI 镜像构建成 ext4 镜像 |
| lifecycle-manager | Go | 控制面 | 空闲暂停、请求到达时唤醒 |
| CubeProxy | OpenResty | 入口 | 把访问沙箱的请求路由到正确的节点和端口 |
| Cubelet | Go | 每台节点 | 节点上的总编排 |
| CubeShim | Rust | 每台沙箱一个进程 | containerd Shim v2，进程里内嵌 VMM |
| CubeHypervisor | Rust | 同上 | 基于 rust-vmm 和 Cloud Hypervisor 的 VMM |
| CubeCoW | Rust | Cubelet 进程内 | XFS reflink 和 S3 两种后端的卷与快照 |
| CubeVS | Go + eBPF | Cubelet 进程内 + 内核 | 每沙箱的 NAT、连接跟踪、出站策略 |
| CubeEgress | OpenResty | 每台节点 | L7 出站代理，域名过滤、凭证注入、审计 |

存储侧还有一个可选的 CubeS3lvol，用 SPDK 把 S3 做成写时复制的块设备，供跨节点暂停恢复使用，放在存储篇里讲。

## 控制面：北向 HTTP，南向 gRPC

请求从 SDK 进来，先到 CubeAPI。它用 Axum 写成，自己不连数据库，只做三件事：按 E2B 的接口格式收请求，按配置做 API Key 校验或调用外部鉴权回调，然后用 HTTP 调 CubeMaster 的 `/cube/sandbox` 系列接口。官方架构文档的时序图把这一跳画成了 gRPC，源码里是 HTTP。

CubeMaster 是控制面的核心。它到每台节点 Cubelet 的调用才是 gRPC，端口 9999。它的状态分两处放：

- **MySQL** 是权威存储：模板定义和副本、沙箱规格、暂停快照的绑定关系、数据卷。
- **Redis** 是协调总线：CubeProxy 用的路由表、生命周期事件流、节点指标、按沙箱的操作锁、CubeProxy 副本的注册表。

所以“控制面无状态、一切在 Redis”这个说法不准确。CubeAPI 确实可以随便横向加；CubeMaster 依赖共享的 MySQL 和 Redis，并在进程里缓存一份节点资源视图供调度使用。

0.7.0 之后又拆出了 CubeOps，节点的注册、封锁、版本矩阵都以它为准，WebUI 的后端也从直连 CubeAPI 改成了经过它。TemplateCenter 和 CubeMaster 的分工是“前者管字节，后者管状态”：构建镜像、上传制品由 TemplateCenter 做；构建任务走到哪个阶段、副本分发到哪些节点，由 CubeMaster 记。

## 一次创建请求

```demo
demos/cube-create.html
```

CubeMaster 收到创建请求后，先把模板里的规格、默认超时、网络策略、卷合并进请求，然后交给调度器选节点。

调度流程仿照 Kubernetes：先预选，再让所有启用的过滤器并行过一遍，取交集，然后加权打分，在最高分附近随机挑一台。默认的过滤器是 CPU、内存、模板本地性和“这台节点正在创建的沙箱数”。其中模板本地性最能体现这套系统的特点：只有本机已经有这份模板快照的节点才算候选。因为毫秒级创建的前提就是模板在本地盘上，调度到一台没有模板的节点，就得现场下载、冷启动、拍快照，等于退回慢路径。

选中节点后，CubeMaster 发起 gRPC `Create`。Cubelet 内部的创建是一条配置出来的工作流，分四步：

1. 分配沙箱 ID，确认本机有可用的模板快照。
2. 并行准备资源：用 CubeCoW 从模板克隆一份私有的可写层，从池里取一块 TAP 设备并下发网络策略，挂载数据卷，写本地元数据。
3. 建宿主机侧的 cgroup。
4. 通过 containerd 创建并启动“容器”，containerd 会拉起一个 CubeShim 进程。

步与步之间串行，同一步里的插件并行。第二步里最耗时的操作也只是几次元数据级的调用：reflink 克隆不拷数据；TAP 设备是 Cubelet 启动后在后台预先建好的，默认配置是一池 500 块，每块现建要六十多毫秒，正好省掉。

CubeShim 进程里直接链着 VMM 库。它从快照恢复虚拟机：把内存镜像文件映射进来，接回块设备、网卡和 vsock，恢复 vCPU。客户机里的 agent 本来就在快照里跑着，恢复后通过一个专用的通知设备报告“vsock 已就绪”，Shim 连上它，下发 `CreateSandbox` 把存储和网络配置补齐。

结果沿原路返回。CubeMaster 拿到沙箱的 IP 和端口映射后，并行做两件事：把路由写进 Redis 供 CubeProxy 使用，把沙箱规格写进 MySQL 供以后暂停恢复使用；再往 Redis 的生命周期事件流里追加一条“已创建”。最后 CubeAPI 拼出 E2B 格式的响应。

## 访问沙箱里的服务

创建成功后，SDK 调用 `get_host(port)` 拿到的地址形如 `端口-沙箱ID.域名`。执行代码、读写文件，都是对这个地址发 HTTP 请求，由客户机里的 envd 处理（默认端口 49983）。这条路不经过 CubeAPI，也不经过 vsock，而是走 CubeProxy。

CubeProxy 是一组 OpenResty。它从 Host 头里拆出端口和沙箱 ID；不方便配泛域名时，也支持 `/sandbox/<沙箱ID>/<端口>/...` 这种路径写法。拿到 ID 后查 Redis 里的路由，找到沙箱所在节点和映射出来的主机端口，转发过去。节点网卡上的 eBPF 程序再把这个主机端口改写成沙箱里的监听端口。

为了不让每个请求都打 Redis，lifecycle-manager 会把路由和状态主动推送到每个 CubeProxy 副本的共享内存里。CubeProxy 启动时往 Redis 注册自己并定期心跳，lifecycle-manager 据此发现所有在线副本，不需要静态配置。

## 暂停、恢复与超时

```demo
demos/cube-life.html
```

沙箱可以设空闲超时，单位秒。不传时用 CubeMaster 的 `default_timeout_insec`，仓库默认值是 -1，也就是永不因空闲回收。超时后的动作默认是销毁；设成 `on_timeout=pause` 则改为暂停。

暂停不是让 vCPU 停在原地。Cubelet 让 VMM 依次做暂停、拍快照、删除虚拟机，Shim 进程随之退出，CPU 和内存全部还给宿主机，只留下盘上的内存镜像和可写层快照。恢复则是用同一个沙箱 ID 再走一次创建，只是请求里带上暂停快照的位置。这样设计的好处是恢复和创建共用一条代码路径；配了 S3 后端时，暂停包可以上传到对象存储，恢复也就可以落到另一台节点上。

空闲扫描由 lifecycle-manager 做。沙箱是否活跃，是在 CubeProxy 上看到的：每个代理副本记下每台沙箱最后一次被访问的时间，lifecycle-manager 定期轮询所有副本，取最大值。沙箱的元数据则来自 Redis 里的生命周期事件流。它自己刚重启时有一段宽限期，免得还没收到活跃记录就把所有沙箱当成空闲。Kubernetes 部署默认两个副本，用 Redis 的 `SET NX PX` 租约选出一个 leader 做扫描；另一个副本照样消费事件、照样能处理唤醒请求。每台沙箱在 Redis 里有一个状态键，暂停和恢复都要先抢到它，跨副本的并发操作因此被串行化。

打开 `auto_resume` 后，请求打到一台已暂停的沙箱时，CubeProxy 会调 lifecycle-manager 的唤醒接口，等沙箱恢复后再转发。沙箱正在暂停中时返回 503 并带 `Retry-After`，已经销毁则返回 410。官方文档把典型的恢复耗时写成亚秒到一秒。暂停本身不取消空闲销毁，暂停太久仍会被清理。

调度器默认把暂停的沙箱仍算作占着 CPU 和内存，这样恢复时一定有配额。`paused_resource_release_ratio` 可以把这部分按比例还给调度器，代价是恢复时要重新准入，节点放不下就返回 409。

## 模板从哪里来

模板是这套系统的地基，构建流程放在[存储篇](/blog/cubesandbox-storage/)里展开，这里只说控制面的分工。用户提交一个 OCI 镜像，CubeMaster 建一条构建任务，TemplateCenter 拉镜像、解包、打成 ext4、上传到制品存储，然后 CubeMaster 挑选节点逐台下发副本。每台节点各自下载镜像、冷启动一台临时虚拟机、等配置好的 HTTP 探针返回 2xx，再把内存和磁盘冻结成快照，登记成本地模板并上报给调度器。

运行中的沙箱也可以提交成新模板，提交那一刻的文件和内存一起进模板。克隆和回滚同样建立在快照上：克隆是先给源沙箱拍一份临时快照，再从它创建 N 台；回滚是从目标快照派生一份新的可写层，再从目标内存快照恢复。

## 部署形态

单机一键部署用 systemd 管理所有进程：控制面的 CubeMaster、CubeAPI、CubeOps、TemplateCenter、lifecycle-manager、CubeProxy、WebUI（默认 `:12088`），依赖的 MySQL、Redis、可选的 MinIO，以及计算面的 Cubelet 和 CubeEgress。Shim、VMM、客户机内核和 agent 镜像作为文件安装到节点上，由 containerd 按需拉起。

Kubernetes 部署把控制面做成 Deployment，计算节点上跑四类 DaemonSet：一个大 Pod 装 Cubelet 和 CubeEgress，一个负责安装 Shim、内核和客户机镜像，一个负责准备 KVM 和 XFS，还有一个可选的 PVM 支持。

计算节点有两个硬性要求：能用 KVM（没有裸金属时可以用 PVM），`/data/cubelet` 必须是开了 reflink 的 XFS。CubeCoW 启动时会真的做一次 reflink 试验，不通过就拒绝启动。

## 公开过的数字

README 把单并发冷启动写成 60 毫秒以内，32 GB 以下规格的额外内存开销写成 5 MB 以内。

2026 年 6 月 1 日的官方基准在腾讯云 BMI5 上测得：96 个逻辑核，375 GiB 内存，沙箱规格 2 vCPU / 2 GiB，统计从 `POST /sandboxes` 到状态变成 `running`。

| 并发 | 平均 | p95 | 吞吐 |
| --- | --- | --- | --- |
| 1 | 47.8 ms | 57.4 ms | 17.9 个/秒 |
| 10 | 88.7 ms | 116.9 ms | 101.4 个/秒 |
| 20 | 98.1 ms | 175.8 ms | 180.9 个/秒 |
| 50 | 276.1 ms | 508.4 ms | 147.6 个/秒 |

同一份报告里，1000 台空载沙箱总共占约 25 GiB 内存，单台均摊 21 到 26 MB。这个数比 2 GiB 的规格小得多，原因在虚拟化篇里会讲：内存是按页从快照映射进来的，只读的页在同一模板的所有沙箱之间共享。

## 参考

- 腾讯云，[CubeSandbox 仓库](https://github.com/TencentCloud/CubeSandbox)。本文主要依据 `CubeAPI`、`CubeMaster`、`Cubelet`、`cube-lifecycle-manager`、`CubeProxy` 目录的源码。
- [架构概览](https://cubesandbox.com/zh/architecture/overview)。组件划分；其中 CubeAPI 到 CubeMaster 的协议与源码不符。
- [沙箱生命周期](https://cubesandbox.com/zh/guide/lifecycle)。自动暂停、恢复、暂停配额比例。
- [模板概览](https://cubesandbox.com/zh/guide/templates)。探针何时表示就绪。
- coolli，[核心操作性能基准测试报告](https://cubesandbox.com/zh/blog/posts/2026-06-01-cubesandbox-perf-benchmark)，2026-06-01。
- [v0.7.0：沙箱跨机流动](https://cubesandbox.com/zh/blog/posts/2026-08-28-cubesandbox-v0.7.0-release)，2026-08-28。S3 跨节点暂停恢复、CubeOps。
