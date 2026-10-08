# 沙箱跨机：各家方案与 CubeSandbox 接入 JuiceFS

沙箱的“跨机”指几件事：在节点 A 上暂停的沙箱，到节点 B 上接着跑；在 A 上做的快照，在 B 上起一台新沙箱；A 宕机或要下线，上面的沙箱能换个地方恢复；新模板不必先在每台机器上铺一遍。单机快照已经很快，CubeSandbox 同机从暂停恢复约 150 毫秒。难的是把这份状态交给另一台机器时，速度还接近单机。

本文分三部分。第一部分说跨机要搬什么，以及常见的三类做法。第二部分看 E2B、Kimi AgentENV、DeepSeek DSec、AWS Lambda、Modal、Fly.io Sprites 等系统具体怎么做。第三部分回到 CubeSandbox：先用官方数据分析现在的 CubeS3lvol 方案慢在哪里，再给出一版用 [JuiceFS](/blog/juicefs/) 做跨机层的架构设计，评估 PoC 的难度和风险。CubeSandbox 的代码基于 `e02976ae`（2026-09-30）。

## 跨机要搬的是什么

一台从快照恢复的微虚拟机由四部分组成。下表的大小取自 CubeSandbox 文档里的同一组测试：2 vCPU、2 GiB 内存、4 GiB 可写层，创建后立刻暂停，客户机里没有额外写入。

| 部分 | 内容 | 大小 | 恢复时何时需要 |
|---|---|---|---|
| 内存镜像 | 暂停那一刻的客户机内存 | 逻辑 2 GiB，相对模板改过的约 205 MiB | vCPU 一跑就要，缺页即阻塞 |
| 可写层 | 客户机写过的磁盘块 | 约 4 MiB | 挂根文件系统时 |
| 只读基础镜像与内核 | 模板的根文件系统、内核旁路文件 | 约 1 GiB 加 50 MiB | 启动就要，但同一模板的沙箱共用 |
| 虚拟机状态与描述 | vCPU 寄存器、设备状态、沙箱规格 | KiB 到 MiB | 恢复第一步 |

有两点影响后面所有设计。第一，内存是大头，而且是“立刻就要”的数据；磁盘可以等挂载、等读到再取，内存不行。第二，真正改过的只占一小部分。按“相对模板的差异”来存和传，比整份搬省一个数量级。

还有一条硬约束：内存快照里有 CPU 寄存器和特性位，只能恢复到 CPU 特性、内核版本一致的机器上。CubeSandbox 用 `cpuid_hash` 和宿主内核版本做相等匹配；Modal 干脆按 worker 类型分别做快照。跨机方案只能在兼容的节点之间流动。

## 三类做法

把状态交给另一台机器，大体有三种做法。区别在于“恢复前必须先到位多少数据”，以及“谁维护块在哪里的映射”。

```demo
demos/xnode-patterns.html
```

整包搬运最简单，任何存储都能用，代价是恢复时间随包大小增长，而且通常只带文件系统。块级按需是目前微虚拟机沙箱的主流：只存脏块，恢复时先拿映射表，缺什么取什么。共享文件系统把映射交给文件系统，沙箱这边只管写文件和打开文件。后两类恢复时搬的数据量差不多，差别在于块格式、缓存、回收这些活由谁来做。

## 各家怎么做

### E2B：diff 链、UFFD、NBD 和对等节点

[E2B](https://github.com/e2b-dev/infra/blob/main/docs/ARCHITECTURE.md) 的出发点是“沙箱就是一个被恢复的快照”。模板是开过机的 Firecracker 快照，放在 GCS 或 S3 上；新建、暂停后恢复、分叉走同一条路径。

暂停时，orchestrator 冻结虚拟机，分别对内存和根文件系统求差：内存靠脏页跟踪，磁盘靠每台沙箱自己的写时复制缓存。差异先缓存在本地，再异步上传。每份制品带一个 header，把每个块映射到它所在的 build，于是暂停包只存相对父层改过的块，恢复时沿着 header 拼出完整视图。代码里内存按 4 MiB chunk 取，根文件系统的映射粒度是 4 KiB（`packages/shared/pkg/storage/header`）。

恢复时，内存交给 userfaultfd：Firecracker 不加载内存，缺页事件交给 orchestrator 里的处理器，处理器从本地缓存或远端取到数据后用 `UFFDIO_COPY` 填进去。根文件系统是进程内的 NBD 服务，只读模板之上叠一层写时复制缓存。模板元数据里还存着一份内存预取映射，恢复时提前把热页拉进来。

调度优先放回源节点，那里有本地缓存，一次对象存储读都不用。去别的节点时，先问源节点：orchestrator 之间有一套 gRPC 的 chunk 服务，上传还没完成的 build 可以直接从源节点读，上传完成后切到对象存储（`packages/orchestrator/pkg/sandbox/template/peerclient`）。一个细节说明这条路径打磨得很细：暂停后的内存去重还没完成时，源节点可以用一个临时 build ID 先供数，但这个临时 ID 被限制在源节点内，跨节点的对等读必须等到去重后的持久 header（[#3166](https://github.com/e2b-dev/infra/commit/77f25a0de4f5cf6d375349d0afada5bc28109db9)）。

### Kimi AgentENV 与 DeepSeek DSec：OverlayBD 加 ublk

[AgentENV](/blog/kimi-agentenv/) 把根文件系统和内存快照都做成 OverlayBD 的层文件，经 Linux ublk 变成块设备交给 Firecracker。磁盘是可写层叠在只读层上；内存是一个只读 ublk 设备，Firecracker 以私有映射打开，写过的页才变成匿名页，同一份快照起的多台虚拟机共用一份页缓存。

快照仓库有两种后端：`oss`（S3 兼容对象存储）和 `posix_fs`（共享文件系统的挂载点）。远端的层按块按需读，本地有一个有上限的 `remote_blocks` 缓存，冷数据淘汰；可选 iroh 做节点间 P2P，先问对等节点再回源。发布时内存层和增量可写层用 lz4 压缩，减少跨节点恢复的网络字节；本地层保持原样，本机恢复不付解压开销。还有一个默认关闭的 `memory_startup_pack`：快照提交后记录一段首触页轨迹，恢复时并发预取（[配置参考](https://github.com/kvcache-ai/AgentENV/blob/main/docs/src/configuration/reference.md)）。分叉只能同机，因为共享的是这台机器的页缓存和层文件。

[DSec](/blog/deepseek-dsec/) 的数据源是训练集群已有的 3FS：存储服务器每台 20 块 SSD、两张 400 Gbps RDMA 网卡，CPU 节点经 FUSE 访问。3FS 大块顺序读很快、小块随机 I/O 很差，所以 DSec 定了三条原则：写留在本地，读按需且成批，元数据尽量放本地。容器镜像离线转成 EROFS，元数据下载到本地盘，文件数据留在 3FS；microVM 的可写盘用 OverlayBD 加 ublk，按 256 KiB 从 3FS 拉，存进本地文件做二级缓存。这段 Rust 实现就是 AgentENV 仓库里的 `storage/overlaybd`。

### AWS Lambda SnapStart：512 KiB chunk 和两级缓存

Lambda 的 [SnapStart](https://aws.amazon.com/blogs/compute/under-the-hood-how-aws-lambda-snapstart-optimizes-function-startup-latency/) 在发布函数版本时跑一次初始化，拍一份加密的 Firecracker 快照，切成 512 KiB 的 chunk 存进 S3。恢复时按需取 chunk：worker 本地盘是 L1，一个 chunk 典型 1 毫秒；每个可用区有一组专用缓存机器做 L2，单毫秒级；都没有才回 S3。系统会根据以前的调用记录主动预取。

这套分块和缓存来自 Lambda 的容器镜像按需加载（[ATC ’23 论文](https://www.usenix.org/conference/atc23/presentation/brooker)）：镜像扁平化成 ext4，切成内容寻址的 512 KiB chunk，用收敛加密做跨客户去重。论文给了一周生产数据：67% 的 chunk 从 worker 本地缓存读到，32% 来自可用区缓存，只有 0.06% 回源 S3。可用区缓存命中的中位延迟 550 微秒，回源 S3 中位 36 毫秒。论文还写明本地 agent 当时经 FUSE 暴露块设备，计划换成 userfaultfd。

### Modal：快照文件放在 FUSE 分布式文件服务上

[Modal](https://modal.com/blog/mem-snapshots) 用 gVisor，快照是 gVisor 自己的 checkpoint：进程树、内存映射、文件表，外加文件系统改动。内存页集中在一个 pages 文件里，通常 100 MiB 到 10 GiB。恢复时 gVisor 不等整份文件读完，先恢复内核状态，内存页在后台载入，进程阻塞在哪一页就先载哪一页。

关键是这个 pages 文件从哪来：它就放在 Modal 加载容器镜像用的那套 FUSE 分布式文件服务上。为了不让缺页等网络，Modal 尽早把整份 pages 文件预读进页缓存；最坏情况下，一次缺页要等 FUSE 服务做一次网络读，几十毫秒。效果是 `import torch` 的冷启动从约 5 秒降到 p50 约 1.05 秒。快照按 worker 类型分别制作，因为某些机型缺 `pclmulqdq` 这类指令。

### Fly.io Sprites：直接用改造过的 JuiceFS

[Sprites](https://fly.io/blog/design-and-implementation/) 是 Fly.io 给 agent 用的持久化虚拟机。每台 100 GB 持久盘，存储的根是 S3 兼容对象存储，本地 NVMe 只是读穿缓存，丢了也无所谓。存储栈“按 JuiceFS 的模型组织”，而且现在用的就是“一个改得很厉害的 JuiceFS”：元数据后端重写成 SQLite，用 Litestream 持续复制到对象存储。他们的说法是，Sprite 的持久状态就是一个 URL，换机器、从坏机器上恢复都很简单。

checkpoint 和 restore 只挪元数据，约 300 毫秒，所以被当成日常功能，而不是出事时的逃生口。两点和沙箱快照不同：checkpoint 只包含文件系统的可写层，不包含内存；存储栈跑在每台 VM 自己的根命名空间里，用户代码在里面的一层容器中，故障半径是单台 VM。同一篇文章也提到，Fly Machines 早就有一个把盘放到对象存储上的功能（LSVD），但性能不够跑生产上热的 Postgres，Sprites 面向的是交互式、经常休眠的负载。

### 其他几家

[Daytona](https://www.daytona.io/docs/en/persistence/) 的容器沙箱停止后可以归档：文件系统备份进对象存储，从 runner 上删掉；启动时从备份恢复，原 runner 不可用就换一台，优先挑已经有基础快照的 runner。这是整包搬运，只带文件系统。它的 VM 类沙箱支持带内存的暂停和快照。

[OpenSandbox](/blog/opensandbox/) 的 BatchSandbox 暂停把根文件系统提交成 OCI 镜像推到仓库，恢复时用新镜像重建，进程内存不在里面；FastSandbox 的检查点放进制品库，恢复可能落到另一台 Fastlet。镜像块经 DART 在节点间分发，4 MiB 一块，本地缓存、对等节点、源站三级。

[CodeSandbox](https://codesandbox.stream/blog/cloning-microvms-using-userfaultfd) 讲过同机克隆运行中的 Firecracker：先用写时复制的 mmap，后来改成 userfaultfd 加写保护，子虚拟机缺页时从父虚拟机懒拷贝。

下面把几家的数据通路并排画出来。左边是沙箱看到的设备，右边是持久存储，中间是节点上供数的进程和缓存层。

```demo
demos/xnode-others.html
```

| 系统 | 隔离 | 跨机带不带内存 | 制品格式 | 恢复时怎么取数据 | 缓存与分发 |
|---|---|---|---|---|---|
| E2B | Firecracker | 带 | 内存、磁盘 diff 加逐块 header | UFFD 供内存，NBD 供磁盘 | 本地缓存，源节点 P2P，GCS / S3 |
| Kimi AgentENV | Firecracker | 带 | OverlayBD 层（内存、磁盘同格式） | 只读 ublk 设备，Firecracker 私有映射 | `remote_blocks` 缓存，iroh P2P，OSS 或共享文件系统 |
| DeepSeek DSec | 容器、microVM 等 | 论文没有展开 | EROFS、OverlayBD | 页缓存与内核预读，ublk 按 256 KiB 拉 | 本地盘二级缓存，3FS |
| Lambda SnapStart | Firecracker | 带 | 512 KiB 加密 chunk | 本地 agent 按需取 | worker L1，可用区 L2，S3 |
| Modal | gVisor | 带 | gVisor checkpoint，pages 文件 | gVisor 后台载页，阻塞页优先 | FUSE 文件服务，主动预读进页缓存 |
| Fly.io Sprites | Firecracker | 不带 | JuiceFS 模型，元数据在 SQLite | 文件系统按需读块 | 本地 NVMe 读穿缓存，对象存储 |
| Daytona | 容器、VM | 容器不带 | 文件系统备份 | 整份恢复 | 优先已有基础快照的 runner |
| CubeSandbox（现状） | CubeHypervisor | 带 | CubeS3lvol 导出 | esnap 克隆读穿，NVMe/TCP 回环 | 本地 WAL 与 chunk cache，S3 |

## 几条共同的经验

**写留在本地，读按需。** 没有一家让运行中沙箱的随机写直接打到远端。E2B 的写进本地写时复制缓存，AgentENV 和 DSec 的写进本地可写层，Fly.io 的写进本地 NVMe 再落对象存储。远端只承担“发布出去以后，谁都能读到”。

**内存要惰性加载，还要预取。** 等整份内存到齐再启动，跨机就失去意义。惰性加载有三种实现：userfaultfd（E2B、CodeSandbox）、把块设备当内存文件映射（AgentENV、CubeS3lvol）、把文件系统上的文件当内存文件映射（Modal，以及本文给 Cube 的方案）。光惰性还不够，冷缓存下每次缺页都是一次网络往返，所以各家都有预取：E2B 的预取映射、AgentENV 的 startup pack、Lambda 按历史访问预取、Modal 主动预读整份文件。下面的演示比较一次缺页在几种后端里各要经过哪些层。

```demo
demos/xnode-memory.html
```

**缓存要分层，回源越少越好。** Lambda 的数字最直观：本地加可用区缓存挡住了 99.9% 以上的读。E2B 和 AgentENV 用对等节点做中间层，OpenSandbox 的 DART 也是本地、对等、源站三级。

**能回源节点就回源节点。** E2B 恢复优先放回源节点，CubeSandbox 的 `restoreplace` 也是源节点可调度就永远本机，Daytona 恢复时优先挑已有基础快照的 runner。跨机是兜底，不是常态，所以本机路径的速度不能为跨机牺牲。

**兼容性是硬约束。** CPU 特性、内核版本、虚拟化模式（AgentENV 的 KVM 与 PVM 快照不能混用）不一致就不能恢复。调度器要知道哪些节点之间能流动。

## CubeSandbox 现在怎么跨机

CubeSandbox 的同机路径建立在 XFS reflink 上：模板、沙箱可写层、快照都是 CubeCoW 管理的文件，克隆是一次 `FICLONE`，内存镜像以私有映射打开（见[存储篇](/blog/cubesandbox-storage/)）。reflink 只在同一个文件系统里有效，所以 0.7.0 加了一个 s3 后端 CubeS3lvol，让暂停包能在别的节点恢复。

```demo
demos/xnode-s3lvol.html
```

CubeS3lvol 是一个基于 SPDK 的用户态块存储进程 `s3lvol_tgt`。Cubelet 经 libcubecow 用 JSON-RPC 驱动它创建卷、快照、克隆；卷经 NVMe/TCP 回环导出成宿主机上的 `/dev/nvmeXnY`，再交给虚拟机当盘或内存文件。写先落到本地的 WAL 镜像，再异步刷进 S3，1 MiB 一个对象（`S3LVOL_DEFAULT_CHUNK_SIZE`）。跨机时，源节点 `rcow_export_snapshot` 把快照发布成一份 manifest；目标节点 `rcow_import_lvol` 建一个 esnap 克隆，读穿到导出，写时复制，后台 decouple 把剩下的数据拷完（`CubeS3lvol/module/bdev/s3lvol/vbdev_s3lvol.h`）。也就是说，导入本身已经是惰性的，不是整包下载。

### 数字

官方文档《跨机快照》第 5 节给了一组 xfs 和 s3 的对照。

```demo
demos/xnode-cube-bench.html
```

### 慢在哪里

下面几条结合数据和代码推断，没有在真实集群上逐项拆解过耗时。

1. **s3 后端连同机都慢。** 从模板冷启动 243 毫秒对 56 毫秒，制作快照 2.2 秒对 97 毫秒，暂停 2.5 秒对 106 毫秒。选了 s3 后端，沙箱的可写层就是从包快照克隆出来的 s3lvol 卷（`Cubelet/storage/s3_volume_manager.go`），内存镜像也在 s3lvol 上，只有只读基础镜像留在本地。每次磁盘读写、每次内存缺页都要经过 nvme-tcp 回环和 SPDK，不管它最后有没有跨机。同机暂停后恢复，s3 后端还要先把包里的内存克隆成沙箱私有的卷，因为包对应的 NVMe 设备随时可能被摘掉（`Cubelet/storage/local.go` 的 `pauseResumeClonesLiveMemory`）。
2. **共享要再等几秒。** 暂停返回后，包要等推到 S3、`remote_status` 变成 `ready` 才能跨机，单个包 4.3 到 5.3 秒；同一时刻只能有一个 export。
3. **跨机恢复受并发影响大。** 单并发 836 毫秒，5 并发 3.2 秒，而目标节点在进入 running 之前只拉了 10 到 20 MiB。
4. **基础镜像不在包里。** 包只有 rootfs、memory、metadata 三份。从没跑过这个模板的节点要先拉约 1 GiB 的基础镜像和内核旁路文件，旧版文档里这一步让跨机格子到了 6 到 12 秒。
5. **后端在建模板时锁死。** 只有 `backend=s3` 的模板及其派生物能跨机，xfs 模板不能事后转换，文档说转换工具“后续提供”。
6. **每台节点的固定开销大。** 约 2 核、19 GiB 内存，一块 512 GiB 的稀疏 WAL 镜像，x86 要 AVX2；WAL 镜像的布局一经创建不能调整。
7. **跨机恢复后短时间不能再暂停**，要等 decouple，文档列为已知问题。

前三条是性能，后四条是使用和运维上的约束。归根到底是一个结构选择：为了让包能跨机，把运行中沙箱的整个数据面搬到了远端块存储上。而前面看到的各家经验是反过来的：运行时留在本地，只有发布出去的那份状态放到共享层。

## 用 JuiceFS 做跨机层

JuiceFS 的设计在[上一篇](/blog/juicefs/)里讲过：文件名和块映射放在元数据引擎，文件内容切成不可变的块放在对象存储，客户端用多级缓存补延迟。对这里有用的是四个性质：

- 所有节点挂载后看到同一个命名空间，打开同一个路径就是同一份数据；
- `juicefs clone` 和 `copy_file_range` 只拷元数据，之后谁改谁写新块（写时重定向）；
- close-to-open 一致性：一个节点 `close` 或 `fsync` 之后，另一个节点再 `open` 能看到全部内容；
- 读按需，本地缓存盘兜底。

CubeSandbox 0.7.2 已经带了一个 [JuiceFS 卷插件](https://github.com/TencentCloud/CubeSandbox/tree/master/examples/volume/juicefs)，安装、`format`、元数据库配置、挂载参数都有现成脚本。接入的问题不在部署，在于把它放在存储栈的哪一层。

### 能不能直接把 CubeCoW 的根目录放到 JuiceFS 上

最直接的想法是让 CubeCoW 的 `root_dir` 指向 JuiceFS 挂载点：所有模板、可写层、快照天然在共享存储上，跨机就是换台机器打开同一个文件。JuiceFS 的形态确实和 XFS 很像，但这条路有几个问题。

- **`FICLONE` 不可用。** CubeCoW 启动时会真的做一次 `FICLONE` 探测，失败就拒绝启动。JuiceFS 的 `ioctl` 只认 `FS_IOC_GETFLAGS`、`FS_IOC_SETFLAGS` 这一族，其余一律返回 `ENOTTY`（`pkg/vfs/vfs_unix.go`）。要改成 `copy_file_range`，JuiceFS 把它实现为元数据拷贝：先刷掉两个文件的写缓冲，再在元数据引擎里复制 slice 引用（`pkg/vfs/vfs.go` 的 `CopyFileRange`）。这部分改动不大。
- **运行时 I/O 全部经过 FUSE。** 每台沙箱的可写层都变成 FUSE 上的文件，客户机的每次随机写都要经过 virtio-blk、CubeHypervisor、FUSE 和 juicefs 进程。默认模式下客户机一次 `fsync` 会让 juicefs 先把数据上传到对象存储、再提交元数据，延迟从本地 NVMe 的几十微秒变成一次对象存储往返。`--writeback` 能把它压回本地盘，但这时“已提交”不再等于“已上传”。官方文档写明，上传完成前其他节点读这些数据会出错。
- **碎片和元数据压力。** 随机小写在 JuiceFS 里会产生大量小 slice，触发碎片合并；元数据引擎的写入量随沙箱数线性增长。
- **故障半径变成整台机器。** 一个 juicefs 进程撑起这台机器上所有沙箱的盘和内存，它出问题就是整机故障。

Fly.io 做的事情接近这条路，但它重写了 JuiceFS 的元数据后端，并把存储栈放进每台 VM 自己里面，故障半径是一台 VM；面向的也是交互式、经常休眠的负载，不是对 I/O 延迟敏感的场景。对 CubeSandbox 来说，这等于用 FUSE 换掉 SPDK，重复 s3 后端“运行时搬到远端”的结构问题，所以不作为第一版。

### 推荐的形态：本地 XFS 热层加 JuiceFS 共享层

第一版只让 JuiceFS 承担“让别的节点拿得到”这一件事。运行中的沙箱、同机的创建、暂停、恢复全部留在本地 XFS 上，一行不改；暂停或做快照之后，把包异步发布到 JuiceFS；跨机恢复时，从 JuiceFS 取回小的可写层，内存直接映射 JuiceFS 上的文件。

```demo
demos/xnode-jfs-arch.html
```

每台节点常驻一个 `juicefs mount`，挂在 `/data/cubelet/jfs`，缓存目录放本地 NVMe。JuiceFS 里的布局：

```text
/cube/tpl/<模板ID>/
    rootfs           模板可写层
    memory           模板内存镜像
    base.ext4        只读基础镜像（第二阶段）
    manifest.json
/cube/pkg/<快照或暂停包ID>/
    rootfs           可写层，稀疏拷贝
    memory           clone 模板内存，再写入脏页
    meta/            sandbox_spec.json 等，原样拷贝
    manifest.json    最后写：各文件大小与校验和、父模板、cpuid_hash、内核版本
```

**模板发布一次。** 模板做好时，把 `tpl-<ID>-rootfs` 和 `tpl-<ID>-memory` 拷进 `/cube/tpl/<ID>/`。这是整份上传，但每个模板只做一次。

**暂停包只传改过的块。** 这是整个设计里最顺的一点。CubeSandbox 本机做快照时，内存镜像是“先 reflink 上一份作为基线，再只覆盖改过的页”（[存储篇](/blog/cubesandbox-storage/)）。JuiceFS 上可以用完全相同的方式生成镜像：先 `copy_file_range` 克隆 `/cube/tpl/<ID>/memory`，只动元数据；再把同一批脏页范围 `pwrite` 进去。JuiceFS 写时重定向，只有脏页对应的新块需要上传。可写层只有几 MiB，用 `SEEK_DATA` / `SEEK_HOLE` 跳过空洞整份拷；以后可写层变大了，可以用 XFS 的 FIEMAP 比较它和父文件的物理 extent，只传不共享的那部分。

每个包都以模板为父，不以上一个包为父。这和 CubeCoW 的扁平快照是同一个思路：包之间没有依赖链，删任何一个都不影响别的，也避免链越长、读时要合并的 slice 越多。

**提交协议。** 所有文件先写在 `<ID>.tmp/` 下，逐个 `fsync`，等块上传完、slice 提交进元数据引擎；然后写 `manifest.json`；最后把目录 rename 成 `<ID>`。rename 是一次元数据事务，别的节点要么看不到这个包，要么看到完整的包。Cubelet 这时才上报 `remote_status=ready`。挂载不开 `--writeback`，否则“提交了”不代表“已上传”。

**本地包变成缓存。** 包发布以后，A 上的本地副本只是为了同机恢复更快。磁盘紧张时可以先淘汰已经 ready 的本地包；之后如果恢复又落回 A，就按跨机路径从 JuiceFS 取。

### 跨机恢复

源节点不可调度时，Master 的放置逻辑不变，只是 `CanCrossNode` 多认一种情况：xfs 后端、包已发布到 JuiceFS、`remote_status=ready`。目标节点上的流程：

1. 打开 `/cube/pkg/<ID>/manifest.json` 和 `meta/`，校验 CPU 和内核指纹。close-to-open 一致性保证这时看到的是源节点提交的全部内容。
2. 把 `rootfs` 拷回本地 XFS，名字就是 `sb-<沙箱ID>-rootfs-gen<N>`，几 MiB，毫秒级。之后这台沙箱的磁盘读写、下一次暂停都在本地。
3. 内存 URL 直接指向 `file:///data/cubelet/jfs/cube/pkg/<ID>/memory`，CubeHypervisor 照常以私有映射打开，恢复 vCPU。

下面的演示把一次跨机暂停恢复走一遍。

```demo
demos/xnode-jfs-flow.html
```

### 内存为什么可以直接映射 JuiceFS 上的文件

CubeHypervisor 从快照恢复时，以 `MAP_PRIVATE` 映射内存文件（`hypervisor/vmm/src/memory_manager.rs`）；只读基础镜像走 virtio-pmem，Cubelet 下发 `discard_writes`，同样是私有映射（`hypervisor/vmm/src/device_manager.rs`）。FUSE 上的普通文件支持这种映射：缺页走 FUSE 读，页进宿主机页缓存，客户机写到的页复制成私有匿名页。这和本机 XFS 上的语义完全一样，VMM 一行不用改，同一份包起的多台沙箱也能共用页缓存。

代价在冷缓存。客户机第一次碰到某页，如果本地缓存盘也没有，就是一次对象存储往返，几十毫秒。JuiceFS 未命中时，偏移不为零、长度不超过块四分之一的小读发范围 GET，其他情况取整个 4 MiB 块写进本地缓存（`pkg/chunk/cached_store.go`）。客户机内存访问往往有局部性，一次取回的块会顺带满足后面的缺页。要把等待从 running 之后挪到之前，有三件事可做：

- **记录首触页。** 包发布后，在源节点（或者第一次恢复时）记录恢复后几百毫秒里碰到的页，写进 manifest。AgentENV 的 startup pack、E2B 的预取映射、REAP 这类论文做的都是这件事。
- **恢复前并发预取。** 目标节点拿到 manifest 后，按首触页列表 `juicefs warmup` 或直接并发读，和 vCPU 恢复并行。
- **调度提示。** 同一份包短时间内会被恢复多次的场景（比如从快照批量创建），优先放到缓存里已有这份包的节点上。

另一个代价是依赖关系。这台沙箱活着的时候，它的内存缺页一直依赖 juicefs 进程和元数据引擎。依赖在下一次暂停时自然解除：新的内存镜像写到 B 的本地 XFS。如果不想等，可以在恢复后的后台对整段内存做 `MADV_POPULATE_WRITE`，把所有页变成私有匿名页，代价是失去页缓存共享、多占内存。这适合长期运行、不会再从同一份包起很多台的沙箱。

### 模板分发

s3 后端的包里没有基础镜像，新节点第一次跑某个模板要先拉约 1 GiB。JuiceFS 方案里可以把 `base.ext4` 和内核旁路文件一起发布到 `/cube/tpl/<ID>/`。新节点有两种用法：virtio-pmem 直接私有映射 JuiceFS 上的文件，按需读；或者后台拷进本地 XFS，拷完之前先从 JuiceFS 读。模板注册时也可以对一批节点提前 `juicefs warmup`。这一步放在第二阶段，它顺带解决了“模板要先在每台机器上铺一遍”的问题。

### 要改哪些代码

CubeSandbox 的跨机控制面在 0.7.0 已经搭好：`remote_status` 状态机、放置决策、HostFacts 兼容性匹配、跨机后的源节点清理都有。JuiceFS 方案主要是给 xfs 后端补一个“发布”和一个“导入”，接到这些现成的口子上。

| 位置 | 现在 | 要改什么 |
|---|---|---|
| `CubeMaster/pkg/restoreplace/placement.go` 的 `CanCrossNode` | 只认 `s3` 且 `ready` | 增加 xfs 加 JuiceFS 发布、`ready` |
| `Cubelet/storage/cow/remote.go` 的 `Uploader` | 只有 S3 Store 实现 | 给 xfscow 加一个 JuiceFS 实现：`Upload` 发布包，`UploadStatus` 报进度 |
| `Cubelet/services/cubebox/remote_uuids.go` | 暂停后只对 s3 后端上传 | 开了 JuiceFS 的 xfs 包也触发；`RemoteUUIDs` 里放 JuiceFS 路径 |
| `Cubelet/storage/s3_sandbox_import.go` 的 `CrossNodeSandboxImport` | 后端必须是 s3，三份都 `import_lvol` | 加 JuiceFS 分支：meta 读目录，rootfs 拷回本地，memory 给路径 |
| `Cubelet/storage/local.go` 的 `prefetchRestoreMemoryVolURL` | 跨机时返回导入的 NVMe 设备 | JuiceFS 分支返回 `/data/cubelet/jfs/...` 的文件 URL |
| 暂停时的内存写入 | 只写本地镜像 | 记下脏页范围，供 Publisher 复用 |
| `CubeMaster/pkg/pausesnap`、`templatecenter/artifact_gc.go` | 回收 s3lvol 对象 | 删除包目录前确认没有活着的沙箱还映射着它的 memory |
| 部署 | `examples/volume/juicefs` 有安装和 format 流程 | 每节点常驻一个挂载，由 systemd 或 DaemonSet 管理 |

CubeCoW 和 CubeHypervisor 都不用改。

### 和 CubeS3lvol 对比

| | CubeS3lvol | JuiceFS 共享层 |
|---|---|---|
| 运行中的数据面 | 可写层和内存都在 s3lvol 上 | 仍在本地 XFS |
| 同机性能 | 比 xfs 慢 4 到 20 倍（官方数据） | 与 xfs 相同 |
| 后端选择 | 建模板时锁死，不能转换 | 任何 xfs 模板都能发布，按集群开关 |
| 跨机读取粒度 | 1 MiB 对象，esnap 读穿 | 4 MiB 块，小读可范围 GET |
| 内存后端 | NVMe 设备，同机恢复也要克隆私有卷 | 普通文件，私有映射，页缓存共享 |
| 基础镜像 | 不在包里 | 可以一起发布 |
| 节点开销 | 约 2 核、19 GiB 内存、512 GiB WAL，要 AVX2 | 一个 juicefs 进程、读写缓冲（默认 300 MiB）、缓存盘 |
| 新增依赖 | SPDK、DPDK、nvme-tcp | 元数据引擎，需要高可用 |
| 工具 | 自研 RPC 与脚本 | `juicefs stats`、`info`、`warmup`、`gc` 等现成工具 |

### 预期性能

下面是按组成部分估算的，PoC 的第一件事就是把它们测出来。

- **同机。** 热路径没变，应与 xfs 后端相同：创建约 56 毫秒，暂停约 106 毫秒，恢复约 154 毫秒。s3 后端的同机开销不再存在。
- **共享到 ready。** 上传量约等于脏数据量，同一组测试里约 210 MiB。JuiceFS 默认 20 个并发上传；同机房对象存储带宽在每秒几百 MB 到 1 GB 时，大约是亚秒到一两秒，再加几次元数据事务。和现在的 4 到 5 秒相比能不能快，取决于对象存储，要实测。
- **跨机恢复到 running。** 几次元数据读（Redis 亚毫秒）、拷几 MiB 可写层、CubeHypervisor 恢复。恢复本身不等内存，所以应接近本机恢复加几十毫秒；running 之后的前几秒会因为冷缓存缺页变慢，预取的作用就在这里。

### PoC 怎么做、难不难

难度中等偏低。理由是不新增数据面：VMM 和 CubeCoW 不动，跨机控制面现成，JuiceFS 的部署脚本也现成。真正未知的只有一件事：内存文件放在 FUSE 上时，恢复和缺页的延迟是否可以接受。所以按风险从高到低排：

1. **阶段 0：验证内存映射（2 到 3 天）。** 两台节点挂同一个 JuiceFS，绕开 Cube 控制面，在 A 上拿到一份暂停包，把文件放进 JuiceFS，在 B 上直接调 CubeHypervisor 的 restore，内存 URL 指向 JuiceFS 上的文件。分别测本地缓存冷和热时的恢复耗时、恢复后一段负载的缺页延迟分布，以及 juicefs 进程的 CPU 占用。这一步不过关，后面都不用做。
2. **阶段 1：打通一条路径（约 2 周，1 到 2 人）。** 实现 Publisher、Importer，放开 `CanCrossNode`，补最小的包回收。端到端用例：A 上暂停，隔离 A，在 B 上恢复；从 A 的快照在 B 上创建。用官方文档第 5 节同一套方法测，直接和 s3 后端的表格对比。
3. **阶段 2：生产化（3 到 4 周）。** 首触页记录与预取、模板经 JuiceFS 分发、FIEMAP 增量、可选的内存水化、监控指标、故障注入（kill juicefs、元数据引擎切主、对象存储限速）。

### 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 冷缓存缺页延迟 | running 后头几秒变慢 | 首触页预取，本地缓存盘，调度偏向已有缓存的节点 |
| juicefs 进程退出 | 映射着包内存的沙箱读未缓存页出错 | 用 v1.2 起的平滑升级，不随便重启；关键沙箱恢复后水化 |
| 元数据引擎不可用 | 不能发布、不能跨机恢复，读未缓存的范围也受影响 | Redis 哨兵或 TiKV 等高可用部署，JuiceFS 每小时自动备份元数据 |
| 写到一半被读 | 读到不完整的包 | `.tmp` 目录加 rename 提交，不开 `--writeback` |
| 碎片 | 内存文件 slice 变多，读放大 | 包一律以模板为父，后台挂载跑碎片合并 |
| 删包时还有人在用 | 活着的沙箱缺页失败 | 复用 Master 现有的延迟清理，JuiceFS 回收站兜底 |
| 数据安全 | 所有节点能读所有包，包里有客户机内存 | 按集群分文件系统，开 JuiceFS 静态加密，限制挂载点权限 |
| CPU、内核不一致 | 恢复失败 | 不变，沿用 `cpuid_hash` 和内核版本匹配 |

## 结语

各家的跨机方案收敛到同一个结构：运行时留在本地，发布出去的状态放到共享层，恢复时按需取、提前预取，中间用缓存挡住回源。区别在于共享层自己造还是用现成的：E2B 和 AgentENV 自己定义块格式和映射，Modal 和 Fly.io 用文件系统，DSec 直接复用训练集群的 3FS。

CubeSandbox 现在的 CubeS3lvol 把运行时也搬到了远端块存储上，所以连同机都慢，还带来了模板锁定和每节点的大块资源占用。JuiceFS 的形态和 XFS 很像，适合当这层共享层：模板发布一次，暂停包用“克隆模板加写脏页”生成，和 CubeCoW 本机的做法一一对应；跨机恢复时内存直接映射 JuiceFS 上的文件，VMM 不用改。第一版的工作量集中在 Cubelet 的发布和导入两段代码上，最大的未知是 FUSE 上内存缺页的延迟，值得先花两三天测清楚。

## 参考

- CubeSandbox：[跨机快照](https://cubesandbox.com/zh/guide/cross-node-snapshot)，第 5 节基准与第 6 节已知问题。
- CubeSandbox：[CubeS3lvol README](https://github.com/TencentCloud/CubeSandbox/blob/master/CubeS3lvol/README.md)、`cubecow/docs/s3lvol-rpc.md`、`CubeS3lvol/module/bdev/s3lvol/vbdev_s3lvol.h`。
- CubeSandbox：[JuiceFS 卷插件](https://github.com/TencentCloud/CubeSandbox/tree/master/examples/volume/juicefs)。
- 本站：[CubeSandbox 存储篇](/blog/cubesandbox-storage/)、[JuiceFS](/blog/juicefs/)、[Kimi AgentENV](/blog/kimi-agentenv/)、[DeepSeek DSec](/blog/deepseek-dsec/)、[OpenSandbox](/blog/opensandbox/)、[Firecracker](/blog/firecracker/)。
- E2B：[docs/ARCHITECTURE.md](https://github.com/e2b-dev/infra/blob/main/docs/ARCHITECTURE.md)，[#3166 decouple warm resume from memfile dedup](https://github.com/e2b-dev/infra/commit/77f25a0de4f5cf6d375349d0afada5bc28109db9)。
- Kimi AgentENV：[配置参考](https://github.com/kvcache-ai/AgentENV/blob/main/docs/src/configuration/reference.md)、[按需加载](https://github.com/kvcache-ai/AgentENV/blob/main/docs/src/getting-started/on-demand-loading.md)。
- DeepSeek-AI 等，[DSec](https://arxiv.org/abs/2609.22978)，arXiv:2609.22978。
- Li 等，[DADI / OverlayBD](https://www.usenix.org/conference/atc20/presentation/li-huiba)，USENIX ATC 2020。
- AWS：[Under the hood: how AWS Lambda SnapStart optimizes function startup latency](https://aws.amazon.com/blogs/compute/under-the-hood-how-aws-lambda-snapstart-optimizes-function-startup-latency/)；Brooker 等，[On-demand Container Loading in AWS Lambda](https://www.usenix.org/conference/atc23/presentation/brooker)，USENIX ATC 2023。
- Modal：[Memory snapshots: Checkpoint/restore for sub-second startup](https://modal.com/blog/mem-snapshots)；gVisor：[Checkpoint/Restore](https://gvisor.dev/docs/user_guide/checkpoint_restore/)。
- Fly.io：[The Design & Implementation of Sprites](https://fly.io/blog/design-and-implementation/)、[Sprites Checkpoints](https://docs.fly.io/sprites/concepts/checkpoints)。
- Daytona：[Persistence](https://www.daytona.io/docs/en/persistence/)。
- CodeSandbox：[Cloning microVMs by sharing memory through userfaultfd](https://codesandbox.stream/blog/cloning-microvms-using-userfaultfd)。
- Firecracker：[Handling page faults on snapshot resume](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md)。
- JuiceFS：[克隆文件或目录](https://juicefs.com/docs/zh/community/guide/clone)、[缓存与 writeback](https://juicefs.com/docs/zh/community/guide/cache)、[平滑升级](https://juicefs.com/docs/zh/community/administration/upgrade)。
