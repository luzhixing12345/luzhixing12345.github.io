# JuiceFS：把对象存储格式化成一块共享硬盘

[对象存储](../object-storage/)便宜、耐用、容量没有上限，但它砍掉了文件系统的许多承诺：不能原地改写，改名要逐个复制，列目录要翻页，首字节延迟几十毫秒。可是训练脚本、Spark 作业、`tar`、`vim`、数据库备份工具，都是照着 POSIX 文件系统写的。

[JuiceFS](https://github.com/juicedata/juicefs) 想同时要两边的好处：数据放在对象存储里，对外却是一个完整的、多台机器可以同时挂载的 POSIX 文件系统。它的官方文档打过一个比方：对象存储像一块容量无限的硬盘，JuiceFS 把这块硬盘“格式化”，元数据引擎就是分区表。

这篇文章讲 JuiceFS 是什么、提供哪些功能、内部怎样设计，它怎样和对象存储配合，它的亮点和代价，以及公开测试里它的性能表现。全文有一条主线：**名字交给数据库，字节切成不可变的块交给对象存储，再用客户端缓存把延迟补回来。**

## JuiceFS 是什么

### 一句话定义

JuiceFS 是一个用 Go 写的分布式文件系统。文件内容被切成块存进对象存储，文件名、目录树、权限和“哪个文件由哪些块组成”这类元数据存进一个数据库（Redis、MySQL、PostgreSQL、TiKV 等任选）。客户端把这两样拼在一起，通过 FUSE 挂载成一个本地目录，也可以通过 Hadoop SDK、S3 网关、Kubernetes CSI 等方式访问。

它不是一个“把桶挂成目录”的工具。s3fs、ossfs 这类工具把一个文件映射成一个对象，桶里看到的就是文件本身；JuiceFS 在桶里只存编号的数据块，离开 JuiceFS 客户端，桶里的内容是读不懂的。这个区别决定了它后面几乎所有的优点和代价。

### 来历

JuiceFS 出自 Juicedata（果汁数据），创始人刘洪清（Davies Liu）是豆瓣早期员工，写过开源 KV 存储 Beansdb 和 Python 版的 Spark 克隆 DPark，后来在 Facebook 做 HDFS，又作为早期工程师加入 Databricks。他在 Databricks 时就提出过为云上数据平台自研存储，没有被采纳，就用业余时间写了原型。2017 年 4 月他和豆瓣时的同事苏锐一起创办 Juicedata，同年 10 月推出全托管的 JuiceFS 云服务（[关于我们](https://juicefs.com/zh-cn/about-us/)）。

云服务卖了三年多、2020 年实现盈亏平衡之后，团队在 2021 年 1 月 11 日把 JuiceFS 社区版开源（[2021，JuiceFS 开源啦](https://juicefs.com/zh-cn/blog/company/juicefs-open-source)）。开源版做了一个关键改动：云服务的元数据引擎是自研的，开源版改成插件式，可以直接用现成的数据库。开源时采用 AGPLv3，2022 年 1 月起改为 Apache 2.0，理由是 Hadoop SDK 和 S3 网关让用户开始在它上面开发商业产品，AGPL 和其他开源协议的兼容性成了障碍（[JuiceFS v1.0 发布](https://juicefs.com/en/blog/release-notes/juicefs-release-v1)）。

此后社区版大约每年一个大版本：1.0（2022）、1.1、1.2、1.3（2025 年 7 月，加入 Python SDK）、1.4（2026 年 7 月，加入分层存储和用户组配额），1.3 和 1.4 都是维护 24 个月的长期支持版。项目 README 写明它的设计参考了 Google File System、HDFS 和 MooseFS。今天它在 GitHub 上有一万四千多颗星。

### 社区版和企业版

JuiceFS 有两个版本，架构相同，最大的区别在元数据引擎：

- **社区版**：开源，元数据放在你选的数据库里。本文讲的设计细节，默认都指社区版，代码引用基于 2026 年 10 月的 `main` 分支。
- **企业版**：闭源，可以私有部署或用云服务。元数据由自研的全内存、基于 Raft 的分布式引擎管理，另外有分布式缓存组、跨云镜像等功能。后文讲性能时会单独说明哪些数字来自企业版。

### 三分钟用起来

准备一个桶和一个 Redis，两条命令就能得到一个文件系统：

```shell
# 格式化：把文件系统的配置写进元数据引擎，并在桶里放一个 UUID 标记
juicefs format \
    --storage s3 \
    --bucket https://mybucket.s3.us-west-2.amazonaws.com \
    --access-key AKIA... --secret-key ... \
    redis://192.168.1.6:6379/1 myjfs

# 挂载：任何一台能连上 Redis 和桶的机器都可以执行，同时挂载
juicefs mount -d redis://192.168.1.6:6379/1 /jfs
```

挂载之后，`/jfs` 就是一个普通目录。往里写一个文件，再去桶里看，看不到这个文件，只能看到一个 `chunks/` 目录和一串编号对象，外加一个 `juicefs_uuid` 和存放元数据自动备份的 `meta/`：

```shell
$ aws s3 ls --recursive s3://mybucket/myjfs/
2026-10-08 15:20:01         36 myjfs/juicefs_uuid
2026-10-08 15:21:13    4194304 myjfs/chunks/0/0/1_0_4194304
2026-10-08 15:21:13    4194304 myjfs/chunks/0/0/1_1_4194304
2026-10-08 15:21:13    2097152 myjfs/chunks/0/0/1_2_2097152
```

这三个对象是一个 10 MiB 的文件被切成的三块。为什么这样切、名字怎么来的，是下文的主要内容。

## 它提供哪些功能

| 类别 | 功能 |
| --- | --- |
| 访问方式 | FUSE 挂载（Linux、macOS）、Windows 客户端、Hadoop Java SDK、Python SDK（兼容 fsspec）、S3 网关、WebDAV、Kubernetes CSI Driver |
| 数据存储 | 三十多种对象存储：S3、OSS、COS、GCS、Azure Blob、OBS、MinIO、Ceph RADOS / RGW、Swift 等；也可以用本地目录、HDFS、WebDAV、SFTP 当“对象存储” |
| 元数据引擎 | Redis 及兼容协议的 Valkey、KeyDB；MySQL、MariaDB、PostgreSQL、SQLite；TiKV、FoundationDB、etcd、BadgerDB |
| 文件系统语义 | 完整 POSIX：硬链接、符号链接、扩展属性、mmap、fallocate、BSD 锁和 POSIX 记录锁、POSIX ACL；强一致，原子改名 |
| 数据处理 | LZ4 / Zstd 压缩；客户端静态加密，每块用 AES-256-GCM 或 ChaCha20-Poly1305 加密、密钥再用 RSA 加密，1.4 起支持 SM4-GCM + SM2；传输加密 |
| 数据管理 | 回收站（默认保留 1 天）、`clone` 秒级克隆、目录用量统计、目录 / 用户 / 用户组配额、分层存储、元数据自动备份、`dump` / `load` 迁移 |
| 运维工具 | `sync` 并发同步（类似分布式 rsync）、`warmup` 预热缓存、`stats` / `profile` 实时性能分析、`fsck` / `gc` / `compact` 校验与回收、Prometheus 指标 |

这里面有几个值得单独说的：

- **多协议看到的是同一份数据。** Spark 用 Hadoop SDK 写进去的 Parquet 文件，训练节点可以直接通过 FUSE 当本地文件读，运维用 `aws s3 cp` 通过 S3 网关拷出来，中间不用任何导入导出。
- **`clone` 只拷元数据。** 克隆一个 1 TB 的目录不会复制对象存储里的任何字节，新旧文件共享同一批块，谁被改写，谁就写到新块上。Linux 上内核支持 `copy_file_range` 时，普通的 `cp` 也会走这条路。
- **`sync` 不止服务 JuiceFS。** 它能在任意两种对象存储、本地目录、HDFS、SFTP 之间增量同步，支持多机分布式执行和断点续传，很多人只把它当跨云迁移工具用。

## 架构：只有客户端是 JuiceFS 自己的

一个 JuiceFS 文件系统由三部分组成：客户端、元数据引擎、对象存储。

```demo
demos/jfs-arch.html
```

和 HDFS、CephFS、Lustre 这些传统分布式文件系统对比，JuiceFS 最大的不同在于：**它没有自己的服务器进程。** HDFS 要部署 NameNode 和 DataNode，CephFS 要部署 MDS 和 OSD，JuiceFS 社区版要部署的只有客户端。元数据引擎是一个现成的数据库，可以直接买云上的托管 Redis 或 RDS；对象存储是现成的云服务。所有的文件系统逻辑，包括切块、缓存、压缩、加密、碎片整理、回收站清理，都在客户端里完成。

这是一种“胖客户端”的设计，两个后端各自只承担它最擅长的事：

- **元数据引擎**负责小而频繁、要求低延迟和事务的操作：查文件名、改权限、改名、加锁。数据库天生擅长这些。
- **对象存储**负责大而少、要求吞吐和耐久的操作：按 4 MiB 整块读写数据。对象存储天生擅长这些，而且它的冗余、修复、跨可用区复制都不用 JuiceFS 操心。

文件系统里最难的两件事，元数据的一致性和数据的耐久性，就这样被外包给了两个已经被大规模验证过的系统。JuiceFS 只需要保证客户端把两边的状态维护对。代价是 JuiceFS 的可用性和耐久性取决于两个后端中较差的那个，后面会再谈。

## 文件怎样变成对象：Chunk、Slice、Block

对象存储不能原地改写，一个对象一旦写下就不可改变。而文件系统要支持随机写、覆盖写、追加写。JuiceFS 用三层结构解决这个矛盾（[架构文档](https://juicefs.com/docs/zh/community/architecture)）：

- **Chunk**：逻辑概念。文件按偏移量切成固定的 64 MiB 一段。读写某个偏移时，直接除以 64 MiB 就知道落在哪个 chunk，不用查找。文件长度不变，chunk 的划分就不变。
- **Slice**：逻辑概念。每一次连续写入产生一个 slice，它属于某一个 chunk，不会跨 chunk，所以最长 64 MiB。同一个 chunk 里可以有多个 slice 相互重叠，读的时候以后写的为准。
- **Block**：物理概念。slice 上传时再切成默认 4 MiB 的块，每块是对象存储里的一个对象，也是本地缓存的单位。块一旦上传就永不修改。

点下面的按钮，先顺序写一个 160 MiB 的文件，再在中间覆盖写两次，看看桶里和元数据里分别发生了什么：

```demo
demos/jfs-layout.html
```

### 覆盖写不改旧对象

关键在第二步。在 20 MiB 处覆盖写 16 MiB，JuiceFS 不会去下载 `1_5` 到 `1_8` 这几个旧块、在内存里改好再传回去，那会把 16 MiB 的写放大成整块的读加写。它只是把新数据作为一个新的 slice 4 上传，再在元数据里给 chunk 0 的 slice 列表追加一条记录。旧块原封不动地留在桶里，只是被遮住了。

元数据里的 slice 记录是一个 24 字节的结构（[内部实现](https://juicefs.com/docs/zh/community/internals)）：

```go
type Slice struct {
    Pos  uint32 // 在 chunk 里的起始偏移
    ID   uint64 // 全局唯一的 slice ID
    Size uint32 // slice 的长度
    Off  uint32 // 有效数据在 slice 里的起点
    Len  uint32 // 有效数据长度
}
```

读一个 chunk 时，客户端按写入顺序把 slice 列表从下往上叠，后写的覆盖先写的，没有 slice 覆盖的部分读出来是零（这就是文件空洞）。叠完之后得到一张“每一段该去读哪个对象的哪一截”的表。`juicefs info` 能打印出这张表。

### 对象的名字

块在桶里的名字是 `${fsname}/chunks/${hash}/${sliceId}_${index}_${size}`。`index` 是这一块在 slice 里的序号，默认一个 slice 最多 16 块，所以是 0 到 15；`size` 是块的原始长度。`hash` 默认是 `sliceId/1000000` 和 `sliceId/1000` 两级目录，目的是别让一个前缀下堆太多对象。

slice ID 由元数据引擎里的一个全局计数器分配，客户端每次批量领一段，永不重复。于是**一个对象名一旦出现，它的内容就永远不会变**。这一点后面会反复用到：缓存不需要失效，对象存储的一致性要求降到最低，克隆只要增加引用计数。

### 碎片与压实

这套设计的代价是碎片。一个文件被反复随机改写，chunk 里的 slice 会越积越多，读取时要叠的层数越来越多，元数据也越来越大，桶里还留着大量被遮住的旧块。

JuiceFS 的对策是后台压实：读一个 chunk 时发现它有 5 个以上的 slice，或者写入时 slice 数到了第 100 个的倍数、超过 350 个，客户端就在后台把这个 chunk 的最新视图读出来，写成一个新的 slice，用一个事务替换掉整个列表（[`pkg/meta/base.go`](https://github.com/juicedata/juicefs/blob/main/pkg/meta/base.go)）。旧 slice 的引用计数归零后，它的块被删除；开着回收站时，这些块会保留到回收站过期，以便恢复。也可以用 `juicefs compact` 手动触发。

顺序写的大文件不会有这个问题：每个 chunk 只有一个 slice，这也是 JuiceFS 最擅长的负载。

## 元数据：一张放在数据库里的 inode 表

### 存了些什么

元数据引擎里存的结构和本地文件系统的 inode 表很像，只是换成了数据库的键值或表。以 Redis 为例（[内部实现](https://juicefs.com/docs/zh/community/internals)）：

| 键 | 类型 | 内容 |
| --- | --- | --- |
| `i${inode}` | String | 文件属性：类型、权限、属主、三个时间戳、长度、父目录 |
| `d${inode}` | Hash | 目录项：文件名 → 类型和 inode 号 |
| `c${inode}_${index}` | List | 第 index 个 chunk 的 slice 列表，每项 24 字节 |
| `sliceRef` | Hash | 被多次引用的 slice 的引用计数（克隆、硬链接后才会出现） |
| `lockf${inode}`、`lockp${inode}` | Hash | BSD 锁和 POSIX 记录锁 |
| `allSessions` | Sorted Set | 在线客户端会话和心跳超时时间 |
| `delfiles`、`delSlices` | Sorted Set / Hash | 等待异步清理的文件和 slice |

SQL 引擎里对应的是 `jfs_node`、`jfs_edge`、`jfs_chunk` 等表，TiKV 这类 KV 引擎用 `A${inode}I`、`A${inode}D${name}`、`A${inode}C${index}` 这样带前缀的键。官方估算的元数据体积是 KV 引擎每个文件约 300 字节，关系型数据库约 600 字节，所以一亿个文件放在 Redis 里大约要 30 GB 内存。

打开 `/dir1/dir2/file` 时，客户端从根目录（inode 固定为 1）开始，在每一级目录的目录项里查下一级的名字，最后拿到文件的 inode 和属性。JuiceFS 基于 FUSE 的低层接口实现，内核直接按 inode 和它交互，不用像 HDFS 的 FUSE 那样反复在路径和 inode 之间转换。

### 原子性来自数据库事务

改名、删除、创建、写入后提交 slice，每一个元数据操作都在一个数据库事务里完成：Redis 用 `WATCH` / `MULTI`，SQL 用事务，TiKV 和 FoundationDB 用它们自己的分布式事务。所以 JuiceFS 的改名是原子的，改一个装着一百万个文件的目录名，也只是删一条目录项、插一条目录项。

对象存储里没有任何元数据，元数据引擎是唯一的真相来源。这一点和 Alluxio 那类“架在对象存储上的缓存层”截然不同：后者的数据和元数据仍然以对象存储里的原样为准，直接改了底下的桶，就要想办法同步。

### 引擎怎么选

不同的引擎在性能、规模、可靠性之间取舍，下一节会给出官方的对比数据。大致的经验是：

- **Redis**：最快，单次元数据操作在百微秒量级。全内存，容量受内存限制，单线程模型下写入吞吐也有上限；持久化要开 AOF，`appendfsync always` 才能做到每个事务落盘，主从切换时可能丢最近的写入。适合千万到亿级文件、追求低延迟的场景。1.4 起支持 Redis 6 的客户端侧缓存，进一步减少读请求。
- **MySQL / PostgreSQL**：耗时大约是 Redis 的 2 到 4 倍，但团队通常更熟悉，备份、高可用都有成熟方案。PostgreSQL 在读操作上明显快于 MySQL。
- **TiKV / FoundationDB**：可以水平扩展到数十亿文件，数据用 Raft 存多副本，单次耗时和 MySQL 接近。适合文件数特别多、对可用性要求高的场景。
- **SQLite / BadgerDB**：单机嵌入式，只适合单机使用或试用。

不管选哪个，客户端每小时都会把全部元数据导出一份，压缩后存进对象存储的 `meta/` 目录，万一元数据引擎整个丢了，还能从最近的备份恢复。文件数超过一百万且备份间隔还是默认一小时时，客户端会跳过备份并告警，需要调大 `--backup-meta`。

## 写一个文件要经过哪些步骤

写入路径决定了 JuiceFS 的一致性语义，也解释了为什么它写大文件很快、写海量小文件相对慢（[数据处理流程](https://juicefs.com/docs/zh/community/internals/io_processing)）。

```demo
demos/jfs-write.html
```

### 先上传，再提交

`write()` 只是把数据拷进客户端的读写缓冲区（默认 300 MiB），几微秒就返回。此时写入的那台机器上 `ls -l` 能看到文件在变大，但这只是本地预览，别的机器什么都看不到，因为元数据还没改。

数据在下面几种情况下被冲刷（flush）到对象存储：一个 slice 写满了 64 MiB 的 chunk；slice 已经存在了 5 秒，或者 1 秒内没有新数据；缓冲区快满了；应用调用了 `close()` 或 `fsync()`。攒满 4 MiB 的块不用等这些条件，会提前开始上传，所以顺序写大文件时，上传和写入是重叠进行的。

flush 时，JuiceFS 先把 slice 的所有块并发上传（默认最多 20 个并发，`--max-uploads` 可调），**全部成功之后**，才用一个元数据事务把这个 slice 追加到 chunk 的列表里，同时更新文件长度和修改时间。同一个文件的多个 slice 严格按创建顺序提交（[`pkg/vfs/writer.go`](https://github.com/juicedata/juicefs/blob/main/pkg/vfs/writer.go)）。

这个顺序保证了：**元数据永远不会指向不存在的数据。** 上传失败，元数据就不提交，`close()` 返回错误；客户端在上传和提交之间崩溃，桶里会多出一些没有任何元数据引用的块，`juicefs gc` 能找到并清理它们，但没有任何文件会读到半截内容。

用 `juicefs stats` 观察顺序写 1 GiB 文件，能看到每次 flush 正好是“1 次元数据事务，16 次 PUT，每次 4 MiB”，也就是一个 64 MiB 的 chunk 对应一次提交。

### 写入的反压

缓冲区只有在数据上传成功后才释放。对象存储带宽不够、并发写入太多时，缓冲区会被待上传的 slice 塞满。缓冲区用量超过阈值后，客户端给每次写入加 10 毫秒延迟；超过阈值两倍，新的写入直接暂停，等缓冲区释放。这时候的对策是调大 `--buffer-size` 和 `--max-uploads`，或者承认瓶颈在对象存储。

### 小文件和 writeback 模式

小文件在 `close()` 时上传，一个文件一次 PUT，对象大小就是文件大小，同时还要两次元数据事务（创建、写入）。写海量小文件时，每个文件都要付一次对象存储 PUT 的往返延迟，这是 JuiceFS 最慢的负载之一。JuiceFS 会顺手把这些小于一个块的数据写进本地缓存，接下来读它们会很快。

`--writeback` 模式把顺序反过来：块先写进本地缓存盘的 `rawstaging/` 目录，立刻提交元数据，`close()` 马上返回，上传在后台异步进行。小文件写入因此快得多，但代价很明确：数据上传之前，这台机器的盘坏了，数据就永远丢了；其他机器已经能看到文件，读它时却要等这台机器上传完，上传慢时会超时。官方建议只在解压大量小文件这类临时场景打开。

## 读一个文件：多级缓存

对象存储的首字节延迟通常是几十毫秒，这对随机读、小文件读、反复读的负载都太慢了。JuiceFS 读路径的设计目标，就是尽量别让请求走到对象存储（[缓存文档](https://juicefs.com/docs/zh/community/guide/cache)）。

```demo
demos/jfs-read.html
```

### 数据缓存

一次读请求从上往下依次查找：

1. **内核页缓存**。文件没被修改时直接从内核返回，延迟是微秒级，同一台机器反复读同一个文件可以达到每秒几 GiB。JuiceFS 每次 `open()` 都会检查文件是否被改过，改过就让旧的页缓存失效。
2. **客户端读写缓冲区**。顺序读时，JuiceFS 在内核预读之上做自己更激进的预读（readahead），预读窗口内的块并发下载；随机读到某个块的一小段时，会在后台把整个块拉下来（prefetch），赌接下来会读到相邻的数据。
3. **本地缓存盘**。从对象存储下载的块、写入时小于一个块的数据，都会以块为单位存进 `--cache-dir`，默认上限 100 GiB，磁盘剩余空间低于 10% 时不再增长，满了按类似 LRU 的策略淘汰。缓存盘可以是 NVMe、多块盘、`/dev/shm` 内存盘，甚至局域网里的共享目录。
4. **对象存储**。用 `GetObject` 读整块，或者用 `Range` 只读需要的一段。

缓存块是不可变对象的副本，内容永远不会过时，所以**数据缓存不需要任何失效机制**。文件被别的机器改了，新数据在新的 slice 和新的块里，客户端从新的 slice 列表出发，自然不会去读旧块。JuiceFS 只需要担心本地盘本身出错，为此在每个缓存文件末尾记录校验和，读取时校验（`--verify-cache-checksum`），1.3 起默认连随机读边界所在的那一段也一并校验。

几个和缓存相关的开关很常用：`juicefs warmup` 在训练开始前把数据集预热进缓存；`--cache-partial-only` 只缓存小文件和小范围随机读，适合本地盘吞吐还不如对象存储的机器；随机稀疏地读大文件时，用 `--prefetch=0` 关掉块级预取，避免读放大。

### 元数据缓存

元数据请求要走网络到元数据引擎，同样需要缓存：

- **内核缓存**：文件属性、目录项默认缓存 1 秒（`--attr-cache`、`--entry-cache`、`--dir-entry-cache`），能大幅加速 `lookup` 和 `getattr`。
- **客户端内存缓存**：`--open-cache` 让 `open()` 直接用内存里的属性和 slice 列表，不再查元数据引擎。它默认关闭，因为打开后就不再保证下面要讲的“关闭后打开”一致性；只读或读多写少的训练任务可以放心打开。
- **Redis 客户端侧缓存**：1.4 起，Redis 6 以上可以在元数据地址里加 `client-cache=true`，客户端在本地缓存 inode 属性和目录项，有人修改时由 Redis 主动推送失效通知。

### 企业版的分布式缓存

社区版的缓存只在本机。几十台 GPU 机器读同一个训练集时，每台都要从对象存储拉一遍，对象存储带宽和请求费都翻了几十倍。

企业版用分布式缓存组解决这个问题：挂载时指定相同 `--cache-group` 的客户端组成一个一致性哈希环（带虚拟节点），每个块按哈希归某个成员负责。任何成员读一个块，都去找负责它的那个成员要；那个成员没有，就由它去对象存储下载并缓存。这样整个组的缓存盘拼成一个大缓存池，同一个块在组里只存一份，只从对象存储拉一次。专门的缓存节点和只消费不贡献的业务节点（`--no-sharing`）可以分开部署，缓存集群独立扩缩容（[分布式缓存](https://juicefs.com/docs/zh/cloud/guide/distributed-cache)）。

社区版用户的替代做法，是把 `--cache-dir` 指向一个共享的分布式文件系统，或者在多台机器之间各自预热。

## 一致性与可靠性

### 关闭后打开

JuiceFS 默认提供**关闭后打开**（close-to-open）一致性：一个客户端写完文件并 `close()` 之后，其他客户端再 `open()` 这个文件，一定能读到刚才写的全部内容。这和 NFS 的承诺相同，比对象存储挂载工具常见的“最终会看到”强得多。在写入的那台机器上，写进去的数据立刻就能读到。

它不是本地文件系统那样“写一个字节、全世界立刻看到”。还没 flush 的数据只在写入方的缓冲区里；另一台机器已经打开的文件，在内核属性缓存过期前（默认 1 秒）可能看不到长度变化。依赖跨机器实时可见的程序，应该在写入端 `fsync()`，在读取端重新 `open()`。

其他语义上的保证：改名和所有元数据操作都是原子的；文件被删除后，同一挂载点上已经打开它的进程还能继续读写；支持 BSD 锁（`flock`）和传统的 POSIX 记录锁（`fcntl`），但受 FUSE 内核模块限制，不支持 OFD 锁。JuiceFS 通过了 pjdfstest 全部 8789 项 POSIX 测试，以及 LTP 里大部分文件系统相关的测试（[POSIX 兼容性](https://juicefs.com/docs/zh/community/posix_compatibility)）。

由于 FUSE 不支持 inotify 跨机器通知，`fswatch`、Watchdog 这类监听工具只能看到本挂载点上的修改，别的机器的修改只能靠轮询发现。

### 数据靠谁保证不丢

数据的耐久性就是对象存储的耐久性，S3、OSS、COS 的设计目标都是 11 个 9 以上；元数据的耐久性取决于你选的数据库和它的部署方式。一个用单机 Redis、没开 AOF 的 JuiceFS，丢一次 Redis 就丢掉了所有文件名和块的对应关系，桶里的数据块虽然还在，但已经拼不回文件了，只能从 `meta/` 下的小时级备份恢复。所以生产环境里元数据引擎必须当作数据库来对待：持久化、主从或多副本、定期备份。

此外还有几道保险：回收站默认开启，删除的文件保留 1 天（`--trash-days`）才真正清理，压实替换下来的旧 slice 也会保留同样长的时间；`juicefs fsck` 检查元数据引用的块是否都在桶里；`juicefs gc` 找出桶里没人引用的块。

## 和对象存储怎样结合

对象存储在 JuiceFS 里的角色，像本地文件系统里的块设备，只是这块“盘”的接口是 HTTP，最小写入单位是一个对象。两者结合时，有几个具体问题值得展开。

### 对对象存储的要求很低

JuiceFS 只用到对象存储最基本的几个接口：`PUT`、`GET`（含范围读）、`DELETE`、`HEAD`、`LIST`，大文件上传时用分片上传。它对一致性的要求也很低：因为每个对象名只写一次、永不覆盖，它只需要“新写入的对象能被读到”，从不依赖覆盖写或删除的即时可见。在 S3 还只提供最终一致性的年代，这个设计就让 JuiceFS 能安全地跑在它上面。

所以 JuiceFS 几乎能对接任何东西：公有云对象存储、MinIO、Ceph RADOS、Swift，甚至本地目录、HDFS、SFTP、WebDAV、Redis、TiKV 和 SQL 数据库都实现了它的对象存储接口（`pkg/object` 目录下有五十多个实现）。

### 绕开对象存储的短处

[对象存储那篇文章](../object-storage/)讲过它的几个短处，JuiceFS 对每一个都有对策：

- **不能原地改写**：改写变成写新 slice、上传新块，见上文。
- **改名要逐个复制**：改名只改元数据，桶里一个字节都不动。
- **`LIST` 慢**：列目录完全由元数据引擎回答，从不 `LIST` 桶。只有 `gc`、`fsck`、`destroy` 这类维护命令才会扫描桶。
- **首字节延迟高**：多级缓存、预读、预取。
- **单个前缀有请求速率上限**：对象名按 slice ID 分散到多级目录下；`--hash-prefix` 能进一步给对象名加上随机前缀，适合仍要求随机化前缀的对象存储（比如 COS 的文档就这样建议）；`--shards` 能把块按哈希分散到多个桶里，绕开桶级限速。
- **请求要收费**：4 MiB 一块，读写大文件时请求次数比按小 I/O 直接访问少得多；元数据请求全部打到数据库，不产生对象存储请求费。

下面用三个常见操作，对比“一个文件对应一个对象”的挂载工具和 JuiceFS 各自要发出哪些请求：

```demo
demos/jfs-vs.html
```

### 分层存储

1.4 版本把对象存储的存储类型也接进了文件系统。可以给文件或目录设置 0 到 3 的层级，每一层映射到一个存储类型（比如 `STANDARD_IA`、`GLACIER_IR`），新文件继承父目录的层级（[分层存储](https://juicefs.com/docs/zh/community/guide/tiered-storage)）：

```shell
juicefs config redis://localhost --tier 1 --storage-class STANDARD_IA -y
juicefs tier set redis://localhost --tier 1 /jfs/logs/2025 -r
```

对象存储的生命周期规则只能按前缀或标签挑对象，而 JuiceFS 的对象名里没有文件路径，所以以前没法做“把这个目录转低频”。分层存储由 JuiceFS 在上传时直接指定存储类型，也可以给对象打上自定义标签，再配合桶的生命周期规则把对象转进归档层，省掉直接以归档类型上传的高额请求费。

### 失去了什么

JuiceFS 的这些好处都来自“不按原样存文件”，代价也在这里：**桶里的数据只有通过 JuiceFS 才能读懂。** 已有的桶不能直接挂载，要先用 `juicefs sync` 导入一遍；别的系统想直接读桶里的文件，必须经过 JuiceFS 的 S3 网关（它基于 MinIO 的网关实现，把 JuiceFS 当作 MinIO 的一块“本地盘”）。

另一条路线是保持一个文件对应一个对象，把文件系统视图“盖”在已有的桶上：s3fs、ossfs 这类 FUSE 工具，AWS 的 Mountpoint for S3，以及 2026 年 4 月推出、用 EFS 作为元数据和小文件缓存层的 S3 Files。这条路线的好处是零迁移，桶里的数据谁都能直接读；坏处是改名、改写、追加这些操作仍然受对象存储语义的约束。选哪条路，取决于数据是要被“当作文件系统用”，还是“偶尔用文件系统的方式看一眼”。

## 亮点

把上面的设计归纳一下，JuiceFS 最有特色的地方有这几处：

- **没有数据服务器。** 社区版只有客户端，元数据引擎和对象存储都可以用云上的托管服务，运维的东西少到只剩一个数据库。扩容不用迁移数据，对象存储本身就是无限的。
- **不可变块。** 一个设计同时解决了随机写、对象存储一致性、缓存一致性、快照克隆四个问题。覆盖写不读旧数据，缓存不用失效，克隆只加引用计数。
- **元数据和数据彻底分离。** 改名、列目录、`stat` 都只和数据库打交道，速度取决于数据库，而不是对象存储。
- **元数据引擎可插拔。** 从 SQLite 单机试用，到 Redis 追求延迟，到 TiKV 扩展到数十亿文件，同一套客户端和数据格式。不同引擎之间可以用 `dump` / `load` 迁移。
- **一份数据，多种协议。** POSIX、HDFS、S3、WebDAV 访问同一份数据，打通大数据平台和 AI 平台之间的数据搬运。
- **缓存贴近计算。** 缓存在客户端本机，用的是计算节点闲置的 NVMe 和内存。企业版进一步把它们拼成分布式缓存池，吞吐随节点数线性增长，而不是绑定在一套昂贵的存储硬件上。

企业版在社区版的基础上，解决的主要是规模问题：自研的全内存元数据引擎用 Raft 做多机复制，按子树分成多个分区，单个元数据进程 30 GiB 内存能管理约 3 亿个文件，平均请求处理时间在 100 微秒量级（[百亿级文件系统实践](https://juicefs.com/zh-cn/blog/engineering/go-build-billion-file-system)）。2026 年初发布的企业版 5.3 把分区上限提到 1024 个，单个文件系统可以管理 5000 亿个以上的文件，并在分布式缓存里引入了 RDMA（[企业版 5.3](https://juicefs.com/zh-cn/blog/release-notes/juicefs-enterprise-5-3-500b-files-rdma-support)）。

## 性能表现

性能数字要先分清三件事：社区版还是企业版，瓶颈在元数据还是数据，测的是冷数据还是缓存命中。下面的数据大多来自 JuiceFS 官方，第三方的生产数据放在最后。

### 元数据：取决于你选的数据库

JuiceFS 官方在同样的客户端、元数据节点和 S3 上，只换元数据引擎，跑了一组对比（[元数据引擎性能测试](https://juicefs.com/docs/zh/community/metadata_engines_benchmark)）。点下面的标签切换指标：

```demo
demos/jfs-bench.html
```

结论大致是：纯元数据操作，MySQL 耗时约为 Redis 的 2 到 4 倍，TiKV 和 MySQL 接近、多数场景略好，etcd 约为 TiKV 的 1.5 倍；小 I/O（约 100 KiB）时，用 MySQL 的总耗时是 Redis 的 1 到 3 倍；**大 I/O（4 MiB）时各引擎没有明显差别**，因为这时瓶颈在对象存储。

拿 Redis 当引擎时，单次 `getattr` 约 90 微秒，`create` 约 0.5 毫秒，读一个有 1000 个文件的目录约 1.5 毫秒。这比本地文件系统慢一个数量级以上，但比直接对对象存储发 `HEAD`、`LIST` 快两个数量级。

### 吞吐：取决于对象存储和网络

在同一组测试里，一台 4 核、最高 10 Gbps 网络的 c5.xlarge 客户端，用 `juicefs bench -p 4` 测大文件，写约 730 MiB/s，读约 900 MiB/s，已经接近网卡上限。大文件顺序读写时 JuiceFS 本身不是瓶颈，带宽上限由客户端网卡、对象存储给单个客户端的带宽和并发数决定；多台客户端并发时，聚合带宽随对象存储的能力扩展。

JuiceFS 文档里还有一组更早的对比：在 c5d.18xlarge 上用 fio 顺序读写 4 GiB 文件，JuiceFS 的吞吐约为 Amazon EFS 和 s3fs 的 10 倍；在 c5.large 上跑 mdtest，JuiceFS 创建文件约 1410 次/秒，EFS 约 179 次/秒，s3fs 不到 6 次/秒（[mdtest 测试](https://juicefs.com/docs/zh/community/mdtest)）。这组测试用的是 Redis 4.0 和 s3fs 1.82，软件版本都很旧，EFS 后来也推出了更高吞吐的模式，适合用来理解架构差异，不适合直接当选型依据。

### AI 训练：MLPerf Storage

MLPerf Storage 是 MLCommons 的存储基准，它模拟真实训练任务的读取模式，要求 GPU 利用率高于阈值（3D U-Net 和 ResNet-50 是 90%，CosmoFlow 是 70%），比的是在这个前提下能喂饱多少块模拟 GPU。2025 年 8 月公布的 v2.0 结果里，JuiceFS（企业版，配合分布式缓存）的成绩是（[MLPerf Storage 结果](https://juicefs.com/docs/cloud/benchmark/mlperf_storage/)）：

| 负载 | 规模 | 读带宽 | 网络带宽利用率 | GPU 利用率 |
| --- | --- | --- | --- | --- |
| 3D U-Net（大文件顺序读） | 10 节点、40 块 H100 | 108 GiB/s | 86.6% | 92.7% |
| ResNet-50（小样本高并发随机读） | 500 块 H100 | 90 GiB/s | 72% | 95% |
| CosmoFlow（海量小文件、对延迟敏感） | 10 节点、100 块 H100 | — | — | 75% |

在 MLPerf Storage 里，这类成绩的意义主要在“网络带宽利用率”：硬件规模可以堆，能把网卡带宽用到多满，才体现软件本身的效率。JuiceFS 在 3D U-Net 和 ResNet-50 里的利用率在基于以太网的方案中最高。

同样来自企业版的数字：100 台 GCP 100 Gbps 节点组成的分布式缓存集群，fio 顺序读的聚合带宽达到 1.2 TB/s，接近 TCP/IP 网络的上限（[企业版 5.2](https://juicefs.com/zh-cn/blog/release-notes/juicefs-enterprise-edition-v52)）。

### 模型加载和生产规模

加载大模型通常是单线程顺序读一个几十 GB 的文件。JuiceFS 团队的测试里，单线程顺序读约 1500 MB/s，针对这个场景优化后能到 3 GB/s；用 PyTorch 加载 26 GB 的 pickle 格式 Llama 2 7B，从内存盘加载是 2.2 GB/s，从 JuiceFS 加载是 2.07 GB/s，达到内存盘的 94%，因为反序列化本身也要时间（[大模型存储实践](https://juicefs.com/zh-cn/blog/solutions/large-model-storage-performance-multi-cloud)）。

生产环境里公开过规模的用户，知乎在社区版上存了 3.5 PB 数据，主要用于机器学习，大模型训练写 checkpoint 时改用企业版（[知乎](https://juicefs.com/zh-cn/blog/user-stories/data-storage-multi-cloud-zhihu-model-training-juicefs)）；携程用 JuiceFS 管理约 10 PB 数据，训练平台读写、推理平台只读挂载同一个卷，省掉了模型分发（[携程](https://cloud.tencent.com/developer/article/2504078)）；理想汽车用它替换了容量紧张的 HDFS，Spark 写进 Hive 表的数据，算法平台的 Pod 直接以 POSIX 方式挂载读取（[理想汽车](https://juicefs.com/zh-cn/blog/user-stories/li-auto-with-juicefs)）。

### 它慢在哪里

最后是 JuiceFS 不擅长的负载，选型时要先测：

- **冷数据的随机小读**：缓存没命中时，每次都要付对象存储几十毫秒的首字节延迟。预取在稀疏随机读时还会造成读放大。
- **海量小文件写入**：每个文件一次 PUT 加两次元数据事务，单客户端每秒几十到几百个文件。`--writeback` 能加速，但要接受丢数据的风险。
- **频繁随机改写**：会产生碎片，读性能下降，要靠后台压实追回来。数据库、虚拟机镜像这类负载更适合块存储。
- **元数据密集且引擎选得不合适**：每个元数据操作都是一次数据库往返，引擎慢、网络远，`ls`、`find`、编译这类操作会明显变慢。
- **FUSE 本身的开销**：每个请求都要在内核和用户态之间切换。

## 局限与取舍

把代价集中说一遍：

- **数据格式私有。** 桶里只有块，必须经过 JuiceFS 读写。格式是开源的，不会被锁死在某个厂商，但会被锁在 JuiceFS 上。
- **元数据引擎是要害。** 它的可用性就是文件系统的可用性，它丢数据就是文件系统丢数据。JuiceFS 把数据库运维外包给了你，或者你的云厂商。
- **一致性是关闭后打开，不是逐字节实时。** 绝大多数应用够用，依赖跨机器实时可见的程序要显式 `fsync` 和重新打开。
- **社区版没有分布式缓存。** 大规模训练读同一份数据时，社区版要么接受对象存储被反复读，要么自己想办法拼共享缓存；这正是企业版的主要卖点。
- **两个后端都要付钱。** 对象存储的容量费和请求费之外，还有一个规格不能太低的数据库。

## 结语

回到开头那句话：名字交给数据库，字节切成不可变的块交给对象存储，再用客户端缓存把延迟补回来。

前半句让 JuiceFS 拿到了一个真正的文件系统：目录树、原子改名、锁、强一致，都由数据库的事务兑现，不再受对象存储语义的约束。中间那句是它最聪明的地方：块不可变、名字不重复，于是覆盖写不用读旧数据，对象存储只需最弱的一致性，缓存永远不会过时，克隆只是加一次引用计数。后半句承认了对象存储慢，然后把计算节点上闲置的内存和 NVMe 变成缓存，让热数据不必每次都回到对象存储。

[对象存储那篇文章](../object-storage/)的结尾说，对象存储当初砍掉的目录、改名、追加、锁和低延迟，正被一样一样加回来，只是不再放在底座里，而是作为架在它上面的一层。JuiceFS 就是这样的一层，而且是其中把文件系统语义补得最完整的之一。

## 参考

**JuiceFS 官方文档与源码**

- [juicedata/juicefs](https://github.com/juicedata/juicefs)，本文代码引用基于 2026-10-08 的 `main` 分支（`28ff9f9`）。
- [架构](https://juicefs.com/docs/zh/community/architecture)、[数据处理流程](https://juicefs.com/docs/zh/community/internals/io_processing)、[内部实现](https://juicefs.com/docs/zh/community/internals)。
- [缓存](https://juicefs.com/docs/zh/community/guide/cache)、[克隆](https://juicefs.com/docs/zh/community/guide/clone)、[分层存储](https://juicefs.com/docs/zh/community/guide/tiered-storage)、[S3 网关](https://juicefs.com/docs/zh/community/guide/gateway)、[数据同步](https://juicefs.com/docs/zh/community/guide/sync)。
- [POSIX 兼容性](https://juicefs.com/docs/zh/community/posix_compatibility)、[规格限制](https://juicefs.com/docs/zh/community/reference/spec-limits)、[元数据备份与恢复](https://juicefs.com/docs/zh/community/metadata_dump_load)、[如何设置元数据引擎](https://juicefs.com/docs/zh/community/databases_for_metadata)。
- [元数据引擎性能测试](https://juicefs.com/docs/zh/community/metadata_engines_benchmark)、[fio 测试](https://juicefs.com/docs/zh/community/fio)、[mdtest 测试](https://juicefs.com/docs/zh/community/mdtest)。
- 对比文档：[JuiceFS 对比 S3FS](https://juicefs.com/docs/zh/community/comparison/juicefs_vs_s3fs)、[对比 Alluxio](https://juicefs.com/docs/zh/community/comparison/juicefs_vs_alluxio)、[对比 CephFS](https://juicefs.com/docs/zh/community/comparison/juicefs_vs_cephfs)、[对比 S3 Files](https://juicefs.com/docs/zh/community/comparison/juicefs_vs_s3files)。
- 版本发布：[v1.3.0](https://github.com/juicedata/juicefs/releases/tag/v1.3.0)（2025-07）、[v1.4.0](https://github.com/juicedata/juicefs/releases/tag/v1.4.0)（2026-07）。

**公司与历史**

- Juicedata，[关于我们](https://juicefs.com/zh-cn/about-us/)。
- Davies，[2021，JuiceFS 开源啦](https://juicefs.com/zh-cn/blog/company/juicefs-open-source)，2021-01-10。
- [JuiceFS v1.0 Officially Released](https://juicefs.com/en/blog/release-notes/juicefs-release-v1)，开源协议从 AGPLv3 改为 Apache 2.0 的说明。
- InfoQ，[不追风口，不依赖风投，盈利后为何选择开源？](https://www.infoq.cn/article/yisapwqsvcvk1j1txkp4)，苏锐 QCon 2024 北京演讲整理。

**企业版与性能**

- [极限挑战：使用 Go 打造百亿级文件系统的实践之旅](https://juicefs.com/zh-cn/blog/engineering/go-build-billion-file-system)。
- [JuiceFS 企业版 5.2](https://juicefs.com/zh-cn/blog/release-notes/juicefs-enterprise-edition-v52)、[JuiceFS 企业版 5.3](https://juicefs.com/zh-cn/blog/release-notes/juicefs-enterprise-5-3-500b-files-rdma-support)。
- [分布式缓存](https://juicefs.com/docs/zh/cloud/guide/distributed-cache)（企业版文档）。
- [MLPerf Storage Benchmark Results for JuiceFS](https://juicefs.com/docs/cloud/benchmark/mlperf_storage/)；MLCommons，[storage_results_v2.0](https://github.com/mlcommons/storage_results_v2.0)。
- [大模型存储实践：性能、成本与多云](https://juicefs.com/zh-cn/blog/solutions/large-model-storage-performance-multi-cloud)。

**用户案例**

- [知乎：多云架构下大模型训练，如何保障存储稳定性](https://juicefs.com/zh-cn/blog/user-stories/data-storage-multi-cloud-zhihu-model-training-juicefs)。
- [稳定且高性价比的大模型存储：携程 10PB 级 JuiceFS 工程实践](https://cloud.tencent.com/developer/article/2504078)。
- [JuiceFS 在理想汽车的使用和展望](https://juicefs.com/zh-cn/blog/user-stories/li-auto-with-juicefs)。
