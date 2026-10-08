# 对象存储：S3、COS 与 OSS

图片、视频、日志、备份、安装包、模型权重、数据湖里的列式文件，今天大多放在对象存储里。Amazon S3、腾讯云 [COS](https://cloud.tencent.com/document/product/436/6222)、阿里云 [OSS](https://help.aliyun.com/zh/oss/user-guide/what-is-oss)，以及开源的 Ceph RGW、MinIO，对外都是同一种形状：一个桶，一堆键，用 HTTP 存取。

这篇文章只讲技术：对象存储是什么，有哪些概念，平时怎么用；它要满足哪些需求；内部由哪些组件拼成，怎样做到又持久又便宜；业界几家方案的异同；以及它今天还没解决好的问题。它是怎样一步步长成今天这样的，另有一篇：[对象存储的发展历史](../object-storage-history/)。

全文只有一条主线：**把“名字”和“字节”分开管，再把字节摊到尽可能多的盘上。** 前半句决定了对象存储的组件怎么切，后半句决定了每个组件要解决什么难题。

## 对象存储是什么

### 三种存储，三种约定

先看对象存储和它的两个邻居有什么不同。

块存储给你一块“盘”。它只认编号：第几号块，写进去 4 KB。这些块拼成了什么文件，它不知道也不关心。数据库、虚拟机的系统盘都跑在块存储上，因为它们需要在任意位置改几个字节，而且默认这块盘只挂在一台机器上。

文件存储在块上面加了一层目录树，并向你承诺很多事：路径可以一层层进入，文件可以在中间随机改写，改名是原子的，多个进程可以加锁。这些承诺都要靠元数据来兑现：每个文件有 inode，每个目录有目录项，改名要同时动两个目录，加锁要有人记住锁在谁手里。在一台机器上，这些都很便宜；放到几千台机器、几千亿个文件上，它们就成了最难扩展的部分。

对象存储把这些承诺砍掉了一大半。对象只能整个上传、整个下载，或者按字节范围读其中一段，不能打开之后跳到中间改 4 个字节。要改内容，就上传一个同名的新对象，把旧的盖掉。没有目录树，没有锁，也不用挂载，任何能发 HTTP 请求的程序都能直接用。

```demo
demos/os-compare.html
```

砍掉这些承诺，换来的是扩展性。没有目录树，创建对象时就不用锁住父目录；没有随机写，一个对象写完就不再变，缓存、复制、校验都简单得多。所以对象存储适合放图片、视频、日志、备份、安装包、模型权重和数据湖里的列式文件，不适合给 MySQL 当数据盘。

### 一个对象由什么组成

一个对象由三样东西组成：桶里唯一的**键**，一段**数据**，以及描述它的**元数据**。对一个对象发 `HEAD` 请求，就能看到除数据以外的全部：

```http
HEAD /photos/2026/a.jpg HTTP/1.1
Host: my-bucket.s3.us-west-2.amazonaws.com

HTTP/1.1 200 OK
Content-Length: 482113
Content-Type: image/jpeg
ETag: "9b2cf535f27731c974343645a3985328"
Last-Modified: Wed, 07 Oct 2026 08:12:40 GMT
x-amz-version-id: 3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY
x-amz-storage-class: STANDARD_IA
x-amz-meta-camera: X100V
```

元数据分两类。系统元数据由服务端维护，比如大小、类型、ETag、修改时间、版本号、存储类型；用户元数据由上传者自己定，以 `x-amz-meta-` 开头，COS 和 OSS 对应的是 `x-cos-meta-` 和 `x-oss-meta-`。元数据和数据一样不可改：想改一个标签，只能把对象“复制到自己身上”并带上新的元数据，相当于重写一次。

## 核心概念

### 地域、可用区与桶

**地域**（region）是一个地理区域，比如 `us-west-2`、`ap-guangzhou`、`cn-hangzhou`；一个地域里有多个**可用区**（AZ），每个可用区是一个或几个独立供电、独立网络的机房。数据默认不出地域，跨可用区冗余是在地域内部做的。

**桶**（bucket）是对象的容器，也是权限、计费、版本控制、生命周期这些配置的挂载点。桶建在某个地域里，名字要全局唯一。访问时桶名通常放进域名，这叫“虚拟主机风格”：

| 服务 | 访问域名 |
| --- | --- |
| S3 | `桶名.s3.地域.amazonaws.com` |
| COS | `桶名-APPID.cos.地域.myqcloud.com` |
| OSS | `桶名.oss-地域.aliyuncs.com` |

把桶名放进域名，DNS 就能按桶把流量引到不同的前端机群，一个特别热的桶不会挤占所有人的入口。另一种“路径风格”把桶名放在路径里（`s3.amazonaws.com/my-bucket/key`），所有桶共用一个域名，S3 已经不推荐新桶使用。

### 键与前缀：桶里是平的

键可以写成 `photos/2026/a.jpg`，控制台也会按 `/` 画出文件夹，但这只是显示效果。桶里其实是平的，`/` 只是键里一个普通字符。元数据层把所有键按字典序排好，“列目录”其实是“列出所有以 `photos/2026/` 开头的键”。

`ListObjectsV2` 有两个参数。`prefix` 是前缀过滤；`delimiter` 是分隔符，通常设成 `/`：在前缀之后、第一个分隔符之前相同的键会被折叠成一条 `CommonPrefixes`，看起来就像一个子目录。结果分页返回，每页最多 1000 条，用 `ContinuationToken` 翻下一页。

```demo
demos/os-keyspace.html
```

这个模型有两个直接后果。其一，“目录”不需要创建，也没有成本：上传 `a/b/c.txt` 时 `a/` 和 `a/b/` 并不存在，删掉最后一个以它开头的键，这个“目录”就消失了。其二，改名没有捷径：把 `a/` 改名成 `c/`，就是对每一个以 `a/` 开头的键做一次“复制到新键，再删旧键”，键越多越慢，而且中间状态对别人可见。

### 版本控制与删除标记

桶默认不开版本控制：同名上传直接覆盖，删除就是真删。开启版本控制以后，每次上传都会生成一个新版本，旧版本仍然保留；不带版本号的 `DELETE` 也不再删数据，而是压上一个“删除标记”（delete marker），让对象看起来不存在。要恢复，删掉这个删除标记即可；要彻底删除，就得指定版本号逐个删。

版本控制是防误删、防勒索的第一道保险，代价是旧版本照样占空间、照样收费，通常要配合生命周期规则定期清理“非当前版本”。

### 存储类型与生命周期

同一个桶里的对象可以属于不同的存储类型。越冷的类型存储单价越低，但读取要另外收费，有最短存储时长，归档类还要先“解冻”（restore）才能读：

| 热度 | S3 | COS | OSS |
| --- | --- | --- | --- |
| 热 | Standard | 标准 | 标准 |
| 温 | Standard-IA、One Zone-IA | 低频 | 低频 |
| 自动 | Intelligent-Tiering | 智能分层 | 按最后访问时间的生命周期规则 |
| 冷 | Glacier Instant / Flexible Retrieval | 归档 | 归档、冷归档 |
| 最冷 | Glacier Deep Archive | 深度归档 | 深度冷归档 |

这些差价反映的是底层真实的成本：越冷的数据可能用更高比例的纠删码、更密的盘，甚至磁带，读一次的代价更高。

对象什么时候变冷，靠人手动挪太累，所以有了生命周期规则。下面这条 S3 规则表示：`logs/` 下的对象 30 天后转低频，90 天后转归档，一年后删除；没完成的分片上传 7 天后清理。

```json
{
  "Rules": [{
    "ID": "logs-tiering",
    "Filter": { "Prefix": "logs/" },
    "Status": "Enabled",
    "Transitions": [
      { "Days": 30, "StorageClass": "STANDARD_IA" },
      { "Days": 90, "StorageClass": "GLACIER" }
    ],
    "Expiration": { "Days": 365 },
    "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
  }]
}
```

### 身份、签名与预签名链接

每个请求都要证明“我是谁”。S3 现在用的是签名 V4（[SigV4](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html)）：客户端用自己的密钥，对请求方法、路径、关键头部、时间和作用范围算出一个 HMAC 签名，附在 `Authorization` 头里；服务端用同一把密钥再算一遍，对得上才放行。密钥本身从不在网络上传输。COS 和 OSS 的签名细节不同，思路一样。

验明身份之后，再看这个身份能做什么。权限有三层：账号里的身份策略（IAM、CAM、RAM），桶上的桶策略，以及逐渐被淘汰的对象 ACL。最常见的事故，是把桶策略设成公开读，或者把一把高权限的长期密钥写进了前端代码。

**预签名链接**把签名提前算好，连同过期时间一起放进 URL 的查询参数里，交给浏览器或另一台机器。在有效期内，拿着链接的人可以做、也只能做签名里写明的那一个操作，不需要知道密钥。用户直传、临时分享下载都靠它，SigV4 的预签名链接最长有效 7 天。

### ETag 与校验和

ETag 是对象内容的一个指纹。普通上传时，它通常就是内容的 MD5；但分片上传完成后，S3 的 ETag 是把各分片的 MD5 拼起来再算一次 MD5，末尾带上分片数，形如 `"…-3"`，和本地对整个文件算出的 MD5 对不上。所以不要拿 ETag 当整个文件的 MD5 用；要端到端校验，应该在上传时指定校验算法（CRC32、CRC64、SHA-256 等），COS 和 OSS 也会在响应里返回 CRC64，供下载端核对。

## 怎么用：几个例子

下面用 AWS CLI 演示。COS 和 OSS 都提供 S3 兼容接口，把 `--endpoint-url` 换成 `https://cos.ap-guangzhou.myqcloud.com` 或 `https://oss-cn-hangzhou.aliyuncs.com`，同样的命令大多能直接跑（OSS 要求使用虚拟主机风格）。

```shell
# 上传，顺带写一个用户元数据和校验算法
aws s3api put-object --bucket my-bucket --key photos/2026/a.jpg \
    --body a.jpg --content-type image/jpeg \
    --metadata camera=X100V --checksum-algorithm CRC32

# 只读前 1 KB：视频拖动进度条、读 Parquet 文件尾部的元数据都靠范围读
aws s3api get-object --bucket my-bucket --key photos/2026/a.jpg \
    --range bytes=0-1023 head.bin

# 列出“photos/ 目录”下的子目录和文件
aws s3api list-objects-v2 --bucket my-bucket --prefix photos/ --delimiter /

# 生成一个 1 小时有效的下载链接
aws s3 presign s3://my-bucket/photos/2026/a.jpg --expires-in 3600
```

### 大文件：分片上传

单次 `PUT` 的大小有上限（S3 是 5 GB），而且一个几十 GB 的文件传到 99% 断了，从头再来代价太大。所以大文件走分片上传：

1. `CreateMultipartUpload`：申请一个上传 ID。
2. `UploadPart`：把文件切成若干分片并行上传，每片带上序号；S3 要求除最后一片外每片至少 5 MB，最多一万片。哪片失败就重传哪片。
3. `CompleteMultipartUpload`：把“序号和各片 ETag”的清单交给服务端，提交成一个对象。

```demo
demos/os-multipart.html
```

关键在第 3 步。那些分片早就躺在存储层里了，“完成”这一下只是在元数据层做一次提交：告诉索引，这些片按这个顺序组成这个键。提交之前，这个对象对外不可见；提交是原子的，别人要么看不到它，要么看到完整的它。没提交的分片照样占空间、照样计费，这就是前面那条生命周期规则里要清理未完成上传的原因。

## 对象存储要满足什么

把上面这套接口做成一个公有云服务，要同时满足几件互相拉扯的事：

- **持久性**：数据不能丢。S3 标准存储的设计目标是 11 个 9，OSS 和 COS 多 AZ 写的是 12 个 9，意思是一年里丢失一个对象的概率在百亿分之一的量级。
- **可用性**：此刻的请求要能成功，通常是 99.99% 到 99.995%。它和持久性是两回事：一个机房断电，数据没丢但暂时读不到，是可用性问题；三块盘同时坏掉而且来不及修，才是持久性问题。
- **扩展性**：对象个数、容量和请求数都不设上限。S3 今天存着超过 500 万亿个对象，每秒处理超过 2 亿个请求（[Twenty years of Amazon S3](https://aws.amazon.com/blogs/aws/twenty-years-of-amazon-s3-and-building-whats-next/)）。
- **吞吐与突发**：一个客户可能平时几乎不动，某一刻突然用几千个函数并行读几百 TB。
- **一致性**：写成功之后，读和列举能不能马上看到。
- **隔离与安全**：一个客户的突发流量不能拖慢别人，一个客户的数据不能被别人读到。
- **成本**：每 GB 每月几分钱。这决定了绝大部分字节只能放在机械硬盘上。

最后一条，是理解对象存储内部设计的钥匙。

### 对手：一块每秒只能随机读写 120 次的硬盘

S3 的杰出工程师 Andy Warfield 算过一笔账：从 1956 年 IBM 的 RAMAC 到当时最大的 26 TB 硬盘，容量涨了约 720 万倍，每字节价格降了约 60 亿倍，随机寻道却只快了约 150 倍。磁头要摆动、盘片要旋转，机械动作快不了多少。一块硬盘拼命做随机读写，大约每秒 120 次。行业路线图指向 200 TB 的硬盘，到那时，如果把随机访问平摊到盘上的全部数据，每 2 TB 数据每秒只能分到 1 次 I/O（[Building and operating a pretty big storage system called S3](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html)）。

把一次随机读拆开：等盘片转到位置，平均约 4 毫秒；磁头平均要移动半个盘面，又是约 4 毫秒；再读出半兆字节的分片，约 2 毫秒。加起来大约 10 毫秒（[Dive deep on Amazon S3](https://www.youtube.com/watch?v=NXehLy7IiPM)）。硬盘还会读错，误码率大约是 10^15 分之一，在 S3 的规模下，这种事“发生得挺频繁”。

这几个数字解释了对象存储的大部分设计：

- 容量便宜、I/O 贵，所以要尽量用大盘，同时让每块盘上的请求尽量少、尽量均匀；
- 顺序写远比随机写便宜，所以单机存储层几乎都采用追加写；
- 硬盘一定会坏、会读错，所以数据必须冗余，而且要不停地检查和修复；
- 单块硬盘很慢，所以一个大请求想快，只能同时动用很多块盘。

Warfield 把单块盘上的请求密度叫作“热度”（heat）。几块盘太热，请求就在那里排队；这种排队还会被上层放大：元数据查找要等它，纠删码凑齐分片也要等它，最后变成少数特别慢的请求，也就是尾延迟。

## 内部架构：一次 PUT 穿过四层

S3 最高一层的结构只有四个方框：面向 HTTP 的前端机群、负责命名空间的索引服务、装满硬盘的存储机群，以及做复制、修复、分层的后台机群。阿里云 OSS 公开的模块几乎一一对应：协议接入层和前端机收请求、做鉴权；分布式 KV“有巢”存元数据；分布式存储系统“盘古”存数据；一致性服务“女娲”负责选主和协调（[OSS 可用性 SLA 技术揭秘](https://developer.aliyun.com/article/766513)）。腾讯云 COS 的底层引擎 YottaStore 分为接入层、元数据缓存层、元数据存储层和数据存储层，另有空间分配、校验修复、数据均衡、健康管理、集群管理五个子系统（[YottaStore 介绍](https://cloud.tencent.com/developer/article/2029420)）。

名字各不相同，切法却是一样的。点下面的按钮，跟着一次上传走一遍：

```demo
demos/os-put.html
```

### 接入层：谁的请求都接，什么状态都不留

请求的第一站是 DNS，返回一组前端机的 IP。S3 故意让不同的客户，甚至同一个客户的不同次解析，拿到不同的一组 IP，这样一个流量特别大的客户不会和别人挤在同一批前端机上。

前端机是无状态的：桶不绑定到特定的服务器，任何一台前端都能服务任何一个桶。这让 SDK 可以放心重试：一次请求失败了或者特别慢，就换个 IP 再发。S3 的 SDK 底层库还会在请求慢到超过 P95 时主动取消、换一台重发（hedged request）。换一台，很可能落到完全不同的前端机和磁盘上，第二次往往更快。

前端机要做的事有三件：验签名、查权限，然后把对象切成分片、算好冗余、发给存储层。YottaStore 的纠删码就在接入层在线完成，数据一进来就编码，直接写到存储节点。

### 元数据层：一张“名字到位置”的表

元数据层记录每个键的当前状态：有没有这个对象，当前是哪个版本，多大，校验值是多少，数据分片在哪些存储节点上。它在 GET、PUT、DELETE 的必经之路上，LIST 和 HEAD 则完全由它来回答（[Diving Deep on S3 Consistency](https://www.allthingsdistributed.com/2021/04/s3-strong-consistency.html)）。

对象存储能“无限扩展”，很大程度上是因为元数据层能切分。它本质上是一个按键排序、按范围分区的分布式 KV：一段连续的键归一个分区管，分区太热或太大，就再切开，分到更多机器上。每个分区有几个副本，副本之间用一致性协议选出主节点，主节点故障时快速换主（[OSS 全栈高可用技术体系解析](https://developer.aliyun.com/article/765215)）。YottaStore 还在元数据存储层前面加了一层缓存，免得高频请求直接打到存元数据的节点上。

分区直接体现在 S3 的性能规则里：每个“已分区的前缀”每秒至少能承受 3500 次写类请求和 5500 次读类请求，前缀的个数不限。某个前缀突然变热，S3 会自动把它切开；切分完成之前，客户端可能暂时收到 `503 Slow Down`（[S3 性能优化](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)）。所以一个要每秒读写几万次的任务，应该把键分散到多个前缀下，而不是全挤在一个前缀里。

元数据层还是“提交点”。分片上传的完成、版本的切换、条件写的比较，都发生在这里。

### 存储层：把字节摊到尽可能多的盘上

存储层拿到分片以后，要决定放在哪些盘上。这里有两条路线。

一条是**记下来**：由元数据层记住每个对象的每一片在哪。S3、OSS、COS 都是这样。代价是多维护一个服务，好处是放置非常灵活，想放哪就放哪，后台也可以随时把数据挪走，只要改一下记录。

另一条是**算出来**：对象的位置由哈希和集群拓扑算出来，不用存。Ceph 的 CRUSH 算法先把对象哈希到某个“放置组”，再按集群拓扑算出这个放置组应该落在哪几个 OSD 上；任何一方只要拿着一张很小的集群图，就能自己算出任何对象在哪（[Ceph, OSDI 2006](https://www.usenix.org/legacy/event/osdi06/tech/full_papers/weil/weil_html/index.html)）。OpenStack Swift 的“环”（ring）也是这个思路（[Swift 架构文档](https://docs.openstack.org/swift/latest/overview_architecture.html)）。这样省掉了一张巨大的位置表，但扩容时要搬哪些数据，由算法决定。

无论哪条路，目标都是把热度摊平。S3 的第一条原则是：同一个桶的数据要散到尽可能多的盘上，每个客户的数据只占每块盘很小的一部分，不同的对象故意放在不同的几组盘上。这叫**洗牌分片**（shuffle sharding）。好处有两个：一是隔离，单个客户再怎么集中访问，也很难压垮某一块盘；二是爆发力，一个客户用几千个函数并行读数据，这波突发可以由超过一百万块盘一起接住。

这背后是一个统计事实：单个工作负载非常“尖”，平时几乎不动，偶尔突然爆发；但成千上万个互不相关的负载叠在一起，总需求就变得平滑、可以预测。规模越大，任何单个负载越左右不了整体的峰值。

挑盘时用的是一个经典技巧：**两次随机选择**（power of two random choices）。完全随机地挑盘，各块盘的用量会慢慢变得不均；每次都查遍所有盘再挑最空的，代价又太高。折中的办法是随机挑两块，放到其中更空的那块上。只看两块盘这么一点信息，效果就已经非常接近全局最优。

分片到了存储节点，由单机存储引擎写进盘里。这里几乎所有人都抛开了通用文件系统：对象又小又多时，一个对象存成一个文件，读一次要先读目录、再读 inode、最后才读数据，三分之二的磁盘操作花在“找”上（[Haystack, OSDI 2010](https://www.usenix.org/conference/osdi10/finding-needle-haystack-facebooks-photo-storage)）。所以单机引擎把许多分片顺序追加进大块的连续区域，自己在内存或 LSM 树里维护“分片 ID 到偏移”的索引。

S3 的 ShardStore 就是这样：索引用 LSM 树，分片正文放在树外的连续区域里以减少写放大，磁盘区域按顺序追加，崩溃一致性用 soft updates 的思路，一次写所依赖的写都落盘之后才发往磁盘，不必每次都走完整的预写日志（[SOSP 2021](https://dl.acm.org/doi/10.1145/3477132.3483540)）。盘古 2.0 用的是只追加的持久化层和“自包含”的块布局，一次写不必分别写数据和元数据两次，数据节点在用户态运行，绕过内核的文件系统和网络栈（[More Than Capacity, FAST 2023](https://www.usenix.org/system/files/fast23-li-qiang_more.pdf)）。YottaStore 用的也是直接操作块设备的自研引擎。

### 后台：一群永远不下班的修补匠

用户收到“上传成功”，这个对象的故事才刚开始。后台机群不在任何请求的返回路径上，却决定了数据能不能存上二十年。

- **巡检与修复**：审计服务持续读取并校验每一个字节。某块盘坏了，或者某个分片读出来校验不对，就用剩下的分片把它算回来，写到别的盘上。持久性的关键不是盘坏得多慢，而是修得比坏得快。
- **均衡**：放置时的判断会过时，数据会变冷，新机架会上线。新机架刚上线时是空的，如果新写入全落在上面，这批新盘就会变成热点；所以要先往新机架搬进一批已有的冷数据，腾出老盘上的空间，让新来的热数据仍然落在尽可能多的盘上。
- **生命周期与数据服务**：到期的对象要删，未完成的分片上传要清理，变冷的对象要转存储类型，跨区域复制要把数据异步拷到另一个地域。
- **空间回收**：对象删除后留下的空洞，要由后台压实回收。

YottaStore 的设计者说得很直白：一个集群想扩展到百万节点，就不能有一个什么都管的 Master。所以这些职能被拆成各自独立扩展的子系统。

## 冗余：副本与纠删码

存储层要回答的第二个问题是：每份数据存几份，怎么存。

### 副本：简单，读得快，但是贵

最直观的办法是多存几份，比如三副本。副本的好处是简单，读的时候还能挑最不忙的那一份，在和硬盘热度的较量里这是很大的优势；修复也便宜，坏了一份照着另一份拷一份就行。缺点是贵：三副本就是三倍的盘。

### 纠删码：用计算换空间

纠删码的思路是：把数据切成 k 份，再用数学方法算出 m 份校验块；这 k+m 份里，任意 k 份都能还原出原始数据。最常用的是 Reed-Solomon 码，记作 RS(k, m)。

S3 的工程师举过一个例子：把一个对象切成 5 个数据分片，再算出 4 个校验分片，一共 9 片，任意 5 片就能拼回整个对象。一个地域通常有三个可用区，每个可用区放 3 片；即使整个可用区不可用，也还剩 6 片，够用。它能容忍任意 4 片同时丢失，开销只有 9/5 = 1.8 倍；用副本达到同样的容错能力，得存 5 份。这只是演讲里的示意，S3 没有公布实际用的 k 和 m，而且它同时使用副本和纠删码，按数据的特点来选。

点击下面的盘让它坏掉，看看坏到第几片时对象就读不回来了：

```demo
demos/os-ec.html
```

纠删码的代价在读和修上。用副本时，读一份就够；用纠删码时，如果存数据分片的某块盘很忙，读请求就得等它，或者绕道去读校验块再把数据算回来。修复更贵：用 RS(12, 4) 时丢了一块，要读另外 12 块才能算回来，网络和磁盘流量是副本方案的很多倍。

**局部重建码**（LRC）专门对付修复成本。微软 Azure Storage 的 LRC(12, 2, 2) 把 12 个数据块分成两组，每组 6 个，各配一个局部校验块，再加 2 个全局校验块。开销是 16/12，约 1.33 倍，但坏了一块只需读同组的 6 块就能修好，修复代价减半（[Erasure Coding in Windows Azure Storage](https://www.usenix.org/conference/atc12/erasure-coding-windows-azure-storage)）。

### 什么时候编码：离线与在线

编码的时机有两种。**离线编码**先以副本形式写入，等数据攒够、封口之后，再由后台慢慢转成纠删码，转完删掉副本。写入路径简单，但要多付一段时间的副本成本，还得维护一套转码流程。**在线编码**在接入层直接编码，编码后的分片直接写进存储节点，中间没有副本缓冲。YottaStore 走的是后一条路，并指出它的另一个好处：离线编码块里的对象被删除后会留下空洞，要等空洞多到一定程度才能整块重写回收；流式的在线编码配合自研单机引擎，可以近乎实时地回收空间。

### 故障域：分片要散开

分片还得散开。放在同一块盘、同一台机器、同一个机架乃至同一个机房里，一次故障就能把数据和校验一起打掉。

S3 标准存储把数据放在一个地域内至少三个可用区。COS 的多 AZ 存储把数据切块、按纠删码生成校验块，再打散到同城三个数据中心的不同机架上，某个机房整体不可用时，其余机房仍能读写（[COS 多 AZ](https://cloud.tencent.com/document/product/436/40548)）。OSS 分为本地冗余（一个可用区内）和同城冗余（跨多个可用区）两种（[OSS 存储类型](https://help.aliyun.com/zh/oss/user-guide/overview-53/)）。S3 的 One Zone 类型、OSS 的冷归档只放在一个可用区里，便宜一些，代价是扛不住整个机房的故障。

## 一致性：写完之后什么时候能读到

### 强一致意味着什么

今天 S3 对所有请求默认提供强一致：`PUT` 成功返回之后，任何人的 `GET`、`HEAD`、`LIST` 都能看到新内容；`DELETE` 成功之后，对象立即不存在（[S3 强一致公告](https://aws.amazon.com/blogs/aws/amazon-s3-update-strong-read-after-write-consistency/)）。OSS 的文档同样写明了原子性和强一致，不会读到写了一半的对象（[什么是 OSS](https://help.aliyun.com/zh/oss/user-guide/what-is-oss)）。COS 的常见问题里写的是“设计保障数据最终一致性”（[COS 一般性问题](https://cloud.tencent.com/document/product/436/30748)），上传成功后能不能立刻读到、覆盖和列举什么时候可见，要以具体接口说明为准，不能直接套用 S3 的承诺。

难点出在元数据层前面那层缓存。缓存是为了性能和高可用加的，但缓存可能拿着旧值。最简单的办法是绕过缓存直接读持久层，但那样会损失性能。S3 的做法借鉴了 CPU 的缓存一致性协议：持久层能推断出每个对象上各次操作的先后顺序；元数据子系统里有一个“见证者”（witness）组件，对象每发生一次变化都会通知它，它只在内存里记很少的状态。读取时，缓存先问见证者自己手里的值有没有过时，没过时就直接返回，过时了就去持久层重新读（[Diving Deep on S3 Consistency](https://www.allthingsdistributed.com/2021/04/s3-strong-consistency.html)）。

### 条件写：对象存储里的“比较并交换”

强一致只保证读到最新的值，还防不住两个写入者互相覆盖。两个程序同时往同一个键写，后到的会悄无声息地盖掉先到的。

条件写解决这个问题。上传时带上 `If-None-Match: *`，如果这个键已经存在，服务端返回 `412 Precondition Failed`，拒绝写入；带上 `If-Match: <ETag>`，只有对象的当前 ETag 还是你之前读到的那个，写入才会成功（[条件写公告](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-functionality-conditional-writes/)）。这就是分布式系统里常说的“比较并交换”（compare-and-swap）。COS 的 `x-cos-forbid-overwrite` 和 OSS 的 `x-oss-forbid-overwrite` 提供的是“禁止覆盖”，相当于其中的前一半。

```demo
demos/os-cas.html
```

很多分布式程序需要一个“谁先提交谁赢”的原子操作，以前只能在对象存储旁边再放一个数据库或锁服务来做。有了条件写，对象存储自己就能当这个裁判。Apache Iceberg、Delta Lake 这类表格式更新一张表时，就是写出新的数据文件和元数据文件，再用一次比较并交换把“当前版本”的指针切过去。

## 业界的方案

### 公有云三家

| | S3 | OSS | COS |
| --- | --- | --- | --- |
| 接入 | 无状态前端机群 | 协议接入层、前端机 | 接入层（在线纠删码） |
| 元数据 | 分区的索引服务，带见证者的缓存 | 分布式 KV 有巢 | 元数据缓存层 + 元数据存储层 |
| 数据 | ShardStore（Rust），洗牌分片 | 盘古 | YottaStore 数据层，自研单机引擎 |
| 协调与后台 | 审计、修复、均衡、复制等后台机群 | 女娲负责选主和协调 | 空间分配、校验修复、数据均衡、健康管理、集群管理 |
| 一致性 | 强一致，支持 `If-Match` 条件写 | 强一致，支持禁止覆盖 | 文档写最终一致，支持禁止覆盖 |
| 特色 | Express One Zone 目录桶、S3 Tables、S3 Vectors | 追加上传 | 追加上传、磁带冷存储引擎 Berg |

三家的分层几乎一样，差别主要在实现和产品边界上。S3 往上长出了专门存 Iceberg 表的表桶、专门存向量的 S3 Vectors，以及单可用区、个位数毫秒延迟的 Express One Zone；它的目录桶有真正的层级命名空间，说明对象存储砍掉的那几刀并不是教条，只要用户愿意付出别的代价，砍掉的东西也可以一样一样加回来。OSS 和 COS 很早就提供追加上传，可以不断往对象末尾追加日志；腾讯在冷数据一端做了支持纠删码的磁带引擎（[QCon 北京 2025](https://qcon.infoq.cn/2025/beijing/presentation/6380)）。

### 开源与私有部署

- **Ceph RGW**：Ceph 的对象网关，底下是 RADOS 对象库，位置由 CRUSH 算出来。成熟、功能全，同一套集群还能提供块存储和文件存储，代价是部署和运维复杂。
- **MinIO**：Go 写的单一可执行文件，几分钟就能跑起一个兼容 S3 的服务，用纠删码集合组织盘，曾经是本地开发和私有部署的默认选择。2025 年它的社区版接连收缩，12 月宣布进入维护模式（[minio/minio#21714](https://github.com/minio/minio/issues/21714)）。
- **SeaweedFS、Garage、RustFS** 等：依赖 MinIO 的项目正在评估的替代方案。SeaweedFS 的设计直接借鉴了 Haystack 的小文件打包思路；Garage 面向跨地域的小规模自建集群。

### “S3 兼容”的坑

S3 的 API 从来没有经过任何标准组织，却成了整个行业的参照。为 S3 写的工具和代码，常常换个地址就能用在别的系统上。但这种兼容是按惯例的兼容，不是按标准的兼容，最容易踩坑的有几处：

- ETag 不一定是 MD5，前面讲过。
- 各种上限不同。S3 单个对象最大 50 TB，一次普通上传最大 5 GB，分片最多一万个（[S3 50 TB](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-maximum-object-size-50-tb/)）；COS 和 OSS 的具体数值不同。
- 扩展功能不通用。追加上传、条件写、版本控制和生命周期规则的细节，各家都有出入。
- 一致性承诺不同。
- 访问风格不同。有的只支持虚拟主机风格，有的私有部署只能用路径风格。

## 目前的问题

对象存储解决了“又多又便宜又不丢”，但用得越广，它砍掉的那些承诺就越扎手。

**元数据是新的瓶颈。** 数据可以摊到百万块盘上，元数据却必须按键排序、精确维护。几十亿个小对象的桶，`LIST` 一遍要翻几百万页；对象越小，元数据开销占比越高，存储引擎里还要为每个对象记索引、算冗余。热点前缀在切分完成前会收到 `503 Slow Down`。S3 推出 S3 Metadata，把桶里对象的元数据整理成可以用 SQL 查询的表，就是为了绕开递归 `LIST`。

**没有目录语义，上层只好自己补。** 改名不是原子的，“目录”下几万个文件要一个个复制再删除；没有追加写（大多数情况下），日志只能攒成大文件再上传；没有锁。数据湖为此发明了表格式，把“哪些文件组成这张表”记在元数据文件里，靠条件写原子切换；AI 训练为此在对象存储上面再架一层文件系统视图或缓存，比如 Mountpoint for S3、S3 Files，以及各种开源的缓存文件系统。

**延迟和尾延迟。** 数据在机械硬盘上，一次随机读就是 10 毫秒级，加上网络、鉴权、元数据查找，首字节延迟通常是几十毫秒，排队时还会更长。对一次读几个 GB 的分析任务这不算问题，对随机读小文件的训练数据加载、对需要毫秒级响应的在线服务就是问题。对策有对冲请求、并行范围读、近计算缓存，以及 Express One Zone 这样用专用硬件换延迟的存储类型，每一种都要付出额外的成本或放弃一部分冗余。

**大盘让 I/O 密度越来越低。** 盘越大，每 TB 分到的 IOPS 越少，坏一块盘要重建的数据也越多，修复时间越长。修复窗口变长，持久性模型里的风险就变大。这逼着系统用更宽的纠删码、更多的盘并行修复、更细的热度管理，也让“存储便宜、访问贵”的定价越来越明显。

**成本模型复杂。** 账单不只按容量算：还有按次数收费的请求费、冷数据的取回费、最短存储时长、小对象的最小计费单位，以及往往比存储本身还贵的公网或跨地域出流量费。一条没想清楚的生命周期规则，把大量小对象转进归档，可能比不转更贵。

**兼容只是表面上的统一。** 一致性承诺、条件写、上限、ETag 计算方式、签名细节，各家都不一样。写一套“S3 兼容”的代码，真正跨云跑起来，往往要为每一家打补丁。开源一侧，MinIO 进入维护模式以后，自建对象存储的选型也重新变得不确定。

## 结语

回到开头那句话：把“名字”和“字节”分开管，再把字节摊到尽可能多的盘上。

前半句决定了组件怎么切。接入层无状态，只负责验明身份、切好分片；元数据层是一张可以不断切分的“名字到位置”的表，一致性和提交都发生在这里；存储层只管把分片可靠地写进盘里；后台负责在盘坏、数据变冷、机架增减的过程中，让这一切持续成立。

后半句决定了每个组件的难题。一块硬盘每秒只能随机读写大约 120 次，所以数据要摊到数百万块盘上，靠规模让负载互不相关；冗余用纠删码把成本压低，再用局部重建码和在线编码把它的副作用压小；单机引擎放弃通用文件系统，改成追加写；修复要比损坏更快。

今天对象存储面临的问题，大多来自它当初砍掉的那些承诺。目录、改名、追加、锁、低延迟，正被一样一样地以新的形式加回来，只是不再放在底座里，而是作为架在它上面的一层。

## 参考

**对象存储的设计与实现**

- Andy Warfield，[Building and operating a pretty big storage system called S3](https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html)，2023-07。四层结构、硬盘物理、热度管理、ShardStore。
- Seth Markle、James Bornholt，[Dive deep on Amazon S3](https://www.youtube.com/watch?v=NXehLy7IiPM)，re:Invent 2024。洗牌分片、两次随机选择、5+4 纠删码示意、新机架的数据均衡。
- Bornholt 等，[Using Lightweight Formal Methods to Validate a Key-Value Storage Node in Amazon S3](https://dl.acm.org/doi/10.1145/3477132.3483540)，SOSP 2021。
- Werner Vogels，[Diving Deep on S3 Consistency](https://www.allthingsdistributed.com/2021/04/s3-strong-consistency.html)，2021-04。
- 亚马逊，[Twenty years of Amazon S3 and building what's next](https://aws.amazon.com/blogs/aws/twenty-years-of-amazon-s3-and-building-whats-next/)，2026-03。
- Weil 等，[Ceph: A Scalable, High-Performance Distributed File System](https://www.usenix.org/legacy/event/osdi06/tech/full_papers/weil/weil_html/index.html)，OSDI 2006。
- OpenStack，[Swift Architectural Overview](https://docs.openstack.org/swift/latest/overview_architecture.html)。
- Beaver 等，[Finding a Needle in Haystack: Facebook's Photo Storage](https://www.usenix.org/conference/osdi10/finding-needle-haystack-facebooks-photo-storage)，OSDI 2010。
- Huang 等，[Erasure Coding in Windows Azure Storage](https://www.usenix.org/conference/atc12/erasure-coding-windows-azure-storage)，USENIX ATC 2012。

**S3 接口与文档**

- [Signature Version 4](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html)。
- [Best practices design patterns: optimizing Amazon S3 performance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)。
- [Amazon S3 Update – Strong Read-After-Write Consistency](https://aws.amazon.com/blogs/aws/amazon-s3-update-strong-read-after-write-consistency/)，2020-12。
- [Amazon S3 adds new functionality for conditional writes](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-functionality-conditional-writes/)，2024-11。
- [Amazon S3 increases the maximum object size to 50 TB](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-maximum-object-size-50-tb/)，2025-12。

**COS 与 OSS**

- 腾讯云，[专有云 TCE COS 新一代存储引擎 YottaStore 介绍](https://cloud.tencent.com/developer/article/2029420)。
- QCon 北京 2025，[腾讯超大规模云原生对象存储引擎架构与实践](https://qcon.infoq.cn/2025/beijing/presentation/6380)。
- 腾讯云，[COS 产品概述](https://cloud.tencent.com/document/product/436/6222)、[多 AZ 特性概述](https://cloud.tencent.com/document/product/436/40548)、[存储类型概述](https://cloud.tencent.com/document/product/436/33417)、[一般性问题](https://cloud.tencent.com/document/product/436/30748)。
- 阿里云，[提升 10 倍！阿里云对象存储 OSS 可用性 SLA 技术揭秘](https://developer.aliyun.com/article/766513)；[阿里云 OSS 从物理到软件的全栈高可用技术体系解析](https://developer.aliyun.com/article/765215)。
- Li 等，[More Than Capacity: Performance-oriented Evolution of Pangu in Alibaba](https://www.usenix.org/system/files/fast23-li-qiang_more.pdf)，USENIX FAST 2023。
- 阿里云，[什么是 OSS](https://help.aliyun.com/zh/oss/user-guide/what-is-oss)、[存储类型概述](https://help.aliyun.com/zh/oss/user-guide/overview-53/)。

**开源**

- MinIO，[Maintenance Mode（minio/minio#21714）](https://github.com/minio/minio/issues/21714)，2025-12。
