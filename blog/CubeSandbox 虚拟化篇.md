# CubeSandbox 虚拟化篇

这是 CubeSandbox 系列的最后一篇，讲虚拟机本身：监视器从哪来、一台沙箱在宿主机上是什么进程、带了哪些设备、怎样在几十毫秒里“启动”，以及内存快照怎样只写改过的页。前三篇分别是[架构篇](/blog/cubesandbox-architecture/)、[存储篇](/blog/cubesandbox-storage/)和[网络篇](/blog/cubesandbox-network/)。

先说结论：生产路径上几乎没有冷启动。沙箱都是从一份内存快照恢复出来的，恢复时内存镜像只映射、不读取；快照时借助 Linux 的 pagemap 和 soft-dirty 只写改过的页，没写的部分由 reflink 继承上一份。下面一层层展开。

## CubeHypervisor 是什么

CubeHypervisor 是从 [Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor) 分叉出来的，底下是 rust-vmm 的那套组件，crate 名改成了 `cube-hypervisor`，版本号停在 28.0.0。顶层的 README 和致谢基本还是上游的样子，真正属于 Cube 的改动集中在下面几处：

- **以库的形式嵌进 Shim。**生产环境不单独起一个 VMM 进程，CubeShim 直接在自己进程里构造一个 VMM 实例，通过进程内的 API 调用它。
- **内置 virtiofsd。**上游的 virtio-fs 需要外挂一个 vhost-user 守护进程；Cube 把 virtiofsd 的透传文件系统直接编进 VMM，设备在进程内完成。
- **三种内存快照模式**：全量、基于 pagemap 的增量、基于 soft-dirty 的增量。内存镜像还可以放在 CubeCoW 管理的独立卷上。
- **“暂停到快照”**：一个操作里依次完成暂停、拍快照、删除虚拟机，供自动暂停使用。
- **SysCtrl 通知设备**：客户机通过它告诉宿主机“agent 的 vsock 已就绪”。x86 上是一个 I/O 端口，ARM64 上是一段 MMIO。
- **PVM 和 ARM64 的适配**：新增 PVM 后端类型，seccomp 规则按架构分开，pagemap 计算按宿主机的真实页大小来。

## 一台沙箱在宿主机上是什么

```demo
demos/cube-vm.html
```

containerd 为每台沙箱拉起一个 `containerd-shim-cube-rs` 进程。对 containerd 来说，它是一个普通的 Shim v2，管着一个“容器任务”；进程里面，Shim 的代码和 VMM 库跑在一起，vCPU 线程、设备线程都属于这个进程。

这里没有 Firecracker 那样的 jailer 进程。隔离靠三层：KVM 的硬件虚拟化；VMM 和它的设备线程跑在 seccomp 系统调用白名单里，Shim 在这个基础上只额外放行少数几个调用；网络出口由 CubeVS 和 CubeEgress 把关。

设备是按沙箱场景挑过的：

| 设备 | 接的是什么 |
| --- | --- |
| virtio-pmem × 3 | 客户机系统镜像、agent 镜像、模板的 OCI 镜像，只读、不回写 |
| virtio-blk | 沙箱私有的可写层，一个 reflink 克隆出来的文件 |
| virtio-net | 宿主机上的 TAP，后面是 CubeVS |
| virtio-vsock | 宿主机一侧是一个 Unix socket，Shim 和 agent 的控制通道 |
| virtio-fs | 宿主机目录和数据卷的共享 |
| virtio-balloon | 默认大小为 0，开启空闲页上报，客户机释放的内存会还给宿主机 |
| SysCtrl | 就绪通知 |

客户机的系统盘是 pmem，不是 virtio-fs。内核参数里写的是 `root=/dev/pmem0` 并以 DAX 挂载，读系统文件直接访问宿主机页缓存里的页。

客户机里的进程分工很清楚。PID 1 是一个很小的 `cube-init`：挂好 `/proc`、`/sys` 和 cgroup2，把第二块 pmem 挂到 `/run/support`，然后 exec 成 `cube-agent`。agent 由 kata-agent 改来，容器管理用的是它带的 rustjail；它在 vsock 端口 1024 上跑 ttrpc，负责建沙箱、挂存储、起停容器、转发标准输入输出。用户看得到的 envd（默认端口 49983）不在这个仓库里，它是模板构建时放进镜像的 E2B 守护进程，用户的命令和文件请求从 CubeProxy 经网络进来，由它处理。

## 恢复，而不是启动

冷启动只在构建模板时发生。那时 Shim 走 `boot` 路径，内核从头引导，`cube-init` 起来、exec agent，容器跑起入口命令，探针通过后整台虚拟机被冻结成快照。

之后每次创建都走恢复路径。Shim 先核对快照里记录的客户机镜像、agent、内核版本和 pmem 布局是否和当前一致，然后组出恢复配置：用哪块 TAP、哪个可写层、哪些共享目录、内存镜像在哪。VMM 据此重建虚拟机，接回各个设备，再恢复 vCPU。

客户机恢复后，agent 已经在快照里的那个状态上跑着。它通过 SysCtrl 报告 vsock 就绪，Shim 收到事件后，连到宿主机侧的 Unix socket，发一行 `CONNECT 1024` 接上 agent 的 ttrpc 服务，然后调 `CreateSandbox` 补齐这一台特有的配置，比如网络和要挂的存储。

为什么这样能快到几十毫秒？因为恢复路径上没有固件，没有内核引导，没有 init 脚本，没有服务初始化；磁盘是 reflink 克隆的；内存是映射进来的，下一节细说。

## virtio-fs：内置的透传

virtio-fs 在这里只做一件事：把宿主机目录共享进客户机，包括用户声明的宿主机挂载和数据卷插件的挂载点。根文件系统不走它。上游文档里“用 virtio-fs 做根文件系统”的教程，不是 Cube 的生产拓扑。

VMM 在进程里直接实例化透传文件系统，缓存策略默认是 never，也就是客户机每次都去宿主机拿最新的内容，保证两边看到的一致。上游文档说 virtio-fs 的 DAX 不可用，指的是 virtio-fs 自己的 DAX 窗口；Cube 用 DAX 的地方是 pmem，两者不是一回事。

恢复时有个细节：Shim 会重新建立宿主机一侧的 virtio-fs 设备，但不会让 agent 再挂一次，因为客户机里的挂载在快照里已经存在，重复挂载会报“设备忙”。

## 内存：MAP_PRIVATE 恢复

```demo
demos/cube-mem.html
```

恢复时，VMM 不会用 `read()` 把几 GiB 的内存镜像灌进一段匿名内存，那样恢复时间会随内存大小线性增长。它用 `mmap(MAP_PRIVATE)` 把镜像文件直接映射到客户机内存的地址上，这一步只建映射，一页也不读。

之后，客户机访问到哪一页，内核才处理哪一页的缺页，结果分三种：

| 客户机做了什么 | 这一页是什么 | 占不占这台沙箱的内存 |
| --- | --- | --- |
| 从没访问过 | 没有页表项 | 不占 |
| 只读过 | 文件页，指向页缓存 | 和同一镜像上的其他沙箱共用 |
| 写过至少一次 | 内核复制出来的私有匿名页 | 这台沙箱独占 |

中间那一行是密度的来源。同一个模板创建的几百台沙箱映射的是同一个内存文件，只读过的页在物理内存里只有一份。官方基准里 2 GiB 规格的空载沙箱只占二十几 MB，原因就在这里。

## 增量快照：哪些页需要写

`MAP_PRIVATE` 还白送了一个性质：**这个进程的匿名页集合，恰好就是“自这次恢复以来客户机写过的页”。**内核在每次写缺页时已经替我们分好了类，不需要任何额外跟踪。

读出这个集合靠 `/proc/self/pagemap`。它为每个虚拟页给出 8 字节，这里只用三位：bit 63 表示页在内存里，bit 62 表示页被换出到了 swap，bit 61 是 `PM_FILE`，表示这一页对应文件或共享内存。增量模式保存的页是“被换出的，或者在内存里但不是文件页的”。官方的深度文章把 bit 61 写成“是不是匿名页”，含义正好说反了，不过结论一样。只看标志位、不看物理页帧号，所以读 pagemap 不需要 `CAP_SYS_ADMIN`，也不怕内核迁移页面。

只写一部分页，怎么得到一份完整的镜像？靠存储层：Cubelet 先把上一份内存镜像 reflink 成新快照的目标文件，VMM 再只覆盖筛出来的那些页。没被覆盖的偏移继续指向上一份的物理块。没有 reflink 提供的这份“廉价基线”，增量就拼不回整份内存。

这对短命的沙箱已经够了。但对长时间运行的沙箱，匿名页只增不减：跑了一小时，可能已经写过十几 GiB，而两次快照之间真正改动的只有几十 MiB，增量模式每次仍要把十几 GiB 全写一遍。

soft-dirty 解决这个问题。内核在每个页表项里有一个 soft-dirty 位，pagemap 用 bit 55 把它暴露出来。往 `/proc/self/clear_refs` 写 `4`，内核会扫一遍进程的页表，清掉所有 soft-dirty 位，并把页表项改成只读；之后客户机再写某一页，会触发一次写保护缺页，内核恢复可写并重新置上 soft-dirty。于是 soft-dirty 模式要写的页是：在内存里、不是文件页、并且 bit 55 为 1，也就是“上次快照之后又写过的页”。

## 什么时候清标记

清标记的时机决定了正确性和开销，代码在这里比较讲究。

**不在恢复时清。**`clear_refs` 要扫整张页表，客户机内存到数 GiB 时要几百毫秒，而且之后的写入都会多一次缺页。如果恢复后立刻清，虚拟机第一次进用户态就会卡住。

**第一次快照按匿名页写。**VMM 里有一个“soft-dirty 是否已开启”的状态，初始为否。第一次收到 soft-dirty 请求时，它先按增量模式把所有匿名页写一遍，作为完整基线；然后探测宿主机内核是否真的支持 soft-dirty（映射一页、写一个字节、看 bit 55 有没有置位），支持就清标记并开启。这样清标记的开销落在一次调用方本来就在等待的快照上。

**之后每次写完再清。**客户机全程是暂停的，顺序固定为：读 pagemap、写出脏页、清标记、恢复运行。如果清标记放在写出之前，写出这段时间里的修改就会丢。清标记失败时，状态退回“未开启”，下一次快照自动按匿名页来写。

这里跟踪的是 VMM 进程自己的页表，soft-dirty 是宿主机内核的功能。客户机内核配置里关着 `CONFIG_MEM_SOFT_DIRTY` 是正常的，和这件事无关。

## 一次快照的完整流程

```demo
demos/cube-snap.html
```

把存储和虚拟化两边合起来，提交一份快照时要做两个选择。

Cubelet 选基线：能找到这台沙箱上一份内存快照的，就 reflink 它作为目标文件，并请求 soft-dirty 模式；血缘断了（快照被删、目录丢失），就新建一个空的内存卷，请求全量模式。新模板的第一份快照总是全量。

VMM 选要写的页：soft-dirty 已开启，写在场、匿名、脏的页；没开启，写在场、匿名的页，然后尝试开启；内核不支持就一直停在匿名页这一级。

这条降级链只会让快照变大，不会让操作失败。可写层的 reflink 在同一个冻结窗口里完成，两边都写完后客户机恢复运行，还是原来的进程、原来的映射。

## 暂停、恢复、回滚、克隆在 VMM 里各是什么

**暂停 vCPU。**VMM 先保存 KVM 时钟（x86 上），再激活还在等待的 virtio 设备，然后依次暂停 vCPU 和所有设备。

**拍快照有两种。**一种是快照后保持冻结，由 Cubelet 接着 reflink 可写层，完成后再恢复运行；Shim 为这段冻结设了租约，内存写得久就续租；超时还没收到恢复指令，Shim 会自己让虚拟机恢复运行，防止沙箱因为上层出错被永远冻住。提交模板、给克隆拍临时快照用的是这种。另一种是“暂停到快照”：暂停、快照、然后直接删除虚拟机，Shim 进程退出，CPU 和内存全部归还，自动暂停用的是这种。

**恢复暂停的沙箱**不是去唤醒一台还在的虚拟机，而是新起一个 Shim，从暂停快照走一遍恢复，再重新接上网络、存储和 vsock。

**回滚**是换一份恢复配置：可写层换成从目标快照派生的新一代，内存镜像换成目标快照，然后恢复。

**克隆**在 VMM 层面就是 N 次独立的恢复。源沙箱只在拍临时快照时短暂暂停，官方文章里这个窗口通常不到 100 毫秒，回来后还是原来的进程。

## 内核、PVM 和 ARM64

客户机内核的配置放在仓库的 `configs/` 下，x86_64 和 aarch64 各一份裁剪过的配置（文件名里的 oc9 指 OpenCloudOS 9），打开了 virtio-fs、vsock、DAX 等必需的功能，x86 的客户机里关掉了 KVM 本身。仓库里有构建脚本，支持交叉编译。

没有裸金属的云服务器，可以用 PVM 运行：这是一种不依赖硬件嵌套虚拟化的方案，宿主机和客户机都要换成带 PVM 补丁的内核，VMM 里对应一个单独的虚拟化后端类型 `KvmPvm`，seccomp 规则与 KVM 共用。官方把裸金属标为性能更好的部署方式。ARM64 上不支持 PVM。原理和代价见[《PVM：没有 VT-x 的云主机上怎样跑 KVM》](/blog/pvm/)。

ARM64 从 0.5.0 起完整构建。除了 SysCtrl 从 I/O 端口改成 MMIO、控制台设备名不同、系统调用号不同之外，最要紧的是页大小：ARM64 宿主机可能用 64 KiB 的页，pagemap 以真实页大小为单位，代码里所有偏移和长度都按 `sysconf(_SC_PAGESIZE)` 计算，写死 4096 会让快照悄悄损坏。很多 ARM64 内核没开 soft-dirty，这时快照会一直停在匿名页增量那一级。

## 和文档对照

- 官方深度文章说 soft-dirty 的第一次快照“不需要任何特殊分支”，靠内核给新页表项默认置位。代码里实际有一个显式的开启状态，第一次快照固定按匿名页写，之后才探测并开启。
- bit 61 的含义是 `PM_FILE`，代码保存的是它为 0 的页。
- CubeShim 的 README 说它“向 Cubelet 请求创建虚拟机”，实际上 VMM 就在 Shim 自己的进程里。
- 有些文档说 agent 是 PID 1。冷启动时先是 `cube-init`，它 exec 之后 agent 才接替 PID 1。

## 参考

- 腾讯云，[CubeSandbox 仓库](https://github.com/TencentCloud/CubeSandbox)。本文依据 `hypervisor`、`CubeShim`、`agent`、`guest-init`、`Cubelet/services/cubebox` 目录的源码，以及 `hypervisor/docs/snapshot_restore.md`。
- sionli，[几十 GiB 快照秒回、克隆“零拷贝”](https://cubesandbox.com/zh/blog/posts/2026-06-25-cubesandbox-snapshot-clone-rollback-deep-dive)，2026-06-25。`MAP_PRIVATE`、pagemap、soft-dirty 与降级链。
- [CubeSandbox v0.3.0：快照、回滚与克隆](https://cubesandbox.com/zh/blog/posts/2026-06-03-cubesandbox-v0.3.0-snapshot)，2026-06-03。
- [ARM64 支持](https://cubesandbox.com/zh/blog/posts/2026-07-08-cubesandbox-arm-support)，2026-07-08。
- [PVM 部署](https://cubesandbox.com/zh/guide/pvm-deploy)。
- Linux 内核文档，[pagemap](https://docs.kernel.org/admin-guide/mm/pagemap.html) 与 [soft-dirty](https://docs.kernel.org/admin-guide/mm/soft-dirty.html)。
