# ICMP：一个被全网屏蔽的信使，是怎样活下来的

1983 年 12 月的一个晚上，马里兰州阿伯丁试验场的弹道研究实验室（BRL）里，Mike Muuss 发现所里的 IP 网络有点不对劲。他想起半年前在挪威的一次 DARPA 会议上，Dave Mills 随口提过一句：他在自己的 Fuzzball 小机器上用一种带时间戳的 ICMP 包测量过网络延迟。Muuss 当即坐下来写了一个小程序，用 IP/ICMP 的回显请求和回显应答去“探”目标机器的远近。程序一次就编译通过，却跑不起来——内核根本不支持原始 ICMP 套接字。他一气之下连内核支持一起写了，天亮之前全部跑通（[The Story of the PING Program](https://ftp.arl.army.mil/~mike/ping.html)）。

这个千行小程序叫 `ping`。四十多年后，它被编进了几乎每一台电脑，从 UNIX 到 Windows。但很少有人意识到，`ping` 只是一个更底层协议的一个用法。那个协议叫 ICMP——Internet Control Message Protocol，互联网控制报文协议。它是 IP 自带的那条“报错与控制”通道：包投递不到时，是谁回一句“目的不可达”？路径太窄、包太大时，是谁喊一声“包太大，发不过去”？`traceroute` 画出的那串途经路由器，又是谁一跳一跳报上来的？答案全是 ICMP。

这篇文章想把 ICMP 的一生讲完：它怎样从 ARPANET 的一个回声指令长成 IP 不可分割的一部分；`ping` 和 `traceroute` 怎样从它身上长出来；它怎样一次次被当成攻击的帮凶——死亡之 Ping、Smurf 洪水——以至于“在防火墙上把 ICMP 全禁了”成了无数网管的肌肉记忆；这条肌肉记忆又怎样造出了一种最难查的故障：**ICMP 黑洞**，ping 得通、网页却打不开；最后，IPv6 怎样把这个被全网嫌弃的信使重新扶上正位，让它成了没有就连不上网的东西。

这是一个关于“报信者”的故事。报信者从不被欢迎，却从没真正死掉。

下面是整个故事的路线图，可以先点一遍，心里有个底，再往下读。

```demo
demos/icmp-timeline.html
```

## 1969–1972：ARPANET 的回声——ECO 与 ERP

ICMP 的祖先，比 IP 还要早。

1969 年 ARPANET 连通时，主机之间说的还不是 TCP/IP，而是一套叫 NCP（Network Control Program）的协议。NCP 的“主机—主机协议”里，除了开关连接的指令，还有三条很特别的控制命令，记在 1972 年 1 月那份没有 RFC 编号的标准文档 NIC 8246 里（四十年后它才被补成 [RFC 6529](https://www.rfc-editor.org/rfc/rfc6529) 存档）：

- **ECO（echo，回声）**：一台主机想知道另一台还活不活得过来，就发一条 ECO，带上任意一段测试数据。
- **ERP（echo reply，回声应答）**：收到 ECO 的主机必须尽快把那段数据原样送回来。文档特意规定，没收到 ECO 就不许主动发 ERP。
- **ERR（error，错误）**：在对方的输入里检测到协议错误时发出，带一个错误码和一段数据。

一个“你在吗”、一个“我在”、一个“你发的东西有毛病”。今天 ICMP 的回显请求、回显应答、各种报错，骨架在这里就定好了。`ping` 的曾祖父，是 1972 年两台 ARPANET 主机之间互相喊的一声 ECO。

下面这张示意图里，圆点是 IMP（接口报文处理机，ARPANET 的分组交换机），方框是挂在上面的主机。IMP 只管把消息一跳一跳往下转，ECO 和 ERP 是两端主机之间的对话。图上那个 ABERDEEN，就是十来年后 `ping` 诞生的地方。

```demo
demos/icmp-arpanet.html
```

## 1979–1981：从网关的“类型字段”到 IP 自己的信使

1970 年代末，ARPANET 开始向“网络的网络”演化，IP 诞生了。IP 只管尽力把包送出去，不保证送到，也不解释为什么没送到。可网络里总得有人来解释：这个包为什么被丢了？该走哪条路更近？网关的缓冲区满了怎么办？

最早承担这个角色的，是网关之间的协议。1979 年 8 月，BBN 的 Virginia Strazisar 写了 [IEN 109《How to Build a Gateway》](https://www.rfc-editor.org/ien/ien109.txt)（如何造一个网关。IEN 是和 RFC 并行的另一套早期互联网技术笔记）。里面定义了一组“网关到主机”的消息，每条消息的第一个字节是一个“网关类型（Gateway Type）”字段：类型 3 是目的不可达，类型 4 是源抑制（让你发慢点），类型 5 是重定向（告诉主机走另一个网关更近）。更有意思的是，网关之间还用类型 8 的“回声”和类型 0 的“回声应答”互相探测邻居死活——和 NCP 的 ECO/ERP 一模一样。

请记住这几个数字：**3、4、5、8、0**。它们本来是网关内部协议的编号。

1981 年 4 月，南加州大学信息科学研究所（ISI）的 Jon Postel 把这些散落在网关协议里的控制消息收拢起来，写成一份独立的规范：[RFC 777](https://www.rfc-editor.org/rfc/rfc777)，Internet Control Message Protocol。它直接继承了那几个类型号——目的不可达还是 3，源抑制还是 4，重定向还是 5，回声还是 8、应答还是 0——又补上了超时（11）、参数问题（12）、时间戳（13/14）。五个月后，1981 年 9 月，与 IPv4 规范 [RFC 791](https://www.rfc-editor.org/rfc/rfc791) 同月问世的 [RFC 792](https://www.rfc-editor.org/rfc/rfc792) 定稿了 ICMP，一直沿用至今，编号 STD 5。

RFC 792 开头有几句话，几乎定义了 ICMP 此后四十年的性格，值得原样读一遍（[RFC 792](https://www.rfc-editor.org/rfc/rfc792)）：

> ICMP 虽然用 IP 作为承载、像是一个更高层的协议，但它其实是 IP 不可分割的一部分，必须由每一个 IP 模块实现。

> 这些控制消息的目的，是对通信环境中的问题提供反馈，而不是让 IP 变得可靠。即便有了 ICMP，仍然不保证数据报一定送达，也不保证控制消息一定送回。

> 为了避免“关于消息的消息”无限递归，不对 ICMP 消息再发 ICMP 消息。

三句话，三个基因。第一句说 ICMP 是 IP 的一部分，不是可有可无的附件——这一点会在二十年后被无数防火墙无视，又在 IPv6 里被重新郑重其事地写回去。第二句说它只负责报信，不负责兜底——可靠性是 TCP 的事。第三句“不对 ICMP 再发 ICMP”，看似是防递归的小心思，七年后却会把 Van Jacobson 坑得不轻。

ICMP 的全套消息并不多，核心就这几类：

| 类型 | 名字 | 干什么 |
| --- | --- | --- |
| 0 / 8 | 回显应答 / 回显请求 | `ping` 的一来一回 |
| 3 | 目的不可达 | 网络/主机/端口到不了；代码 4 是“需要分片但设了 DF” |
| 4 | 源抑制 | 让发送方慢下来（后被废弃） |
| 5 | 重定向 | 告诉主机换个网关更近 |
| 11 | 超时 | TTL 减到 0，或分片重组超时 |
| 12 | 参数问题 | 包头有毛病 |
| 13 / 14 | 时间戳请求 / 应答 | 测量延迟用 |

每一条 ICMP 报错里，都会把惹祸的那个原始包的 IP 头、外加头部之后的前 64 位数据一并带回去。为什么是 64 位？因为那正好够装下 IP 头后面 TCP 或 UDP 的端口号。有了它，发送方的主机才能把这条“你的包出问题了”准确对上是哪一条连接。这个看似不起眼的设计，后来会同时成就 `traceroute`，和一类针对 TCP 的盲攻击。（1995 年的路由器要求规范 [RFC 1812](https://www.rfc-editor.org/rfc/rfc1812#section-4.3.2.3) 放宽了限制，让路由器在整条 ICMP 不超过 576 字节的前提下尽量多带一些原始数据。不过发送方能指望的，始终只有那最少的 8 字节。）

下面是 ICMP 报文的格式。切换三种消息看看：回显是一问一答，数据区随便装；两种报错的后半截，都是那个惹祸的原始包的开头。

```demo
demos/icmp-format.html
```

## 1983：Muuss 与 ping——一个晚上写出来的传奇

回到开头那个晚上。Muuss 给他的程序起名 `ping`，取的是声呐的那一声“乒”——他在大学里做过不少声呐和雷达系统的建模，觉得“在网络空间里回声定位”这个类比再贴切不过：发一个带时间戳的 ICMP 回显请求，听回声，算距离（[The Story of the PING Program](https://ftp.arl.army.mil/~mike/ping.html)）。

点几下下面的按钮，日志里打印的是 Muuss 在主页上贴出的那段真实输出。

```demo
demos/icmp-ping.html
```

有意思的是，那天晚上他其实没真正“抓”到网络故障——他还在改内核的时候，同事 Chuck Kennedy 已经先一步把出问题的网络硬件修好了。Muuss 后来打趣说，早知道这玩意儿会成为他这辈子最有名的作品，他当时真该多花一两天，多加几个选项。

Berkeley 的人很快把他的内核改动和 `ping` 源码收了回去，从此 `ping` 成了 BSD UNIX 的标配，再从 BSD 流向全世界。1993 年，也就是 `ping` 问世十年后，USENIX 把终身成就奖颁给了伯克利的 CSRG，Muuss 是联合获奖人之一。至于那个流传甚广的“PING 是 Packet InterNet Groper（网际包探测器）缩写”的说法，Muuss 本人一直坚持这是个声呐类比，不是缩写；但他听说这个“反向缩写”是 Dave Mills 想出来的，于是半开玩笑地说，也许两个人都对。

`ping` 身上还挂着一个互联网最经典的冷笑话。有本 1933 年的童书叫《[The Story About Ping](https://en.wikipedia.org/wiki/The_Story_About_Ping)》，讲一只叫 Ping 的小鸭子在长江上走失又回家的故事。1999 年 3 月，一位自称来自“乌兹别克斯坦上沃尔特”（一个不存在的地方）的读者在亚马逊上一本正经地给它写了篇书评，说作者“用巧妙的寓言，深入浅出地讲解了 UNIX 最古老的网络工具之一”，而且显然拿到的是“非常早期的测试版”，因为这本书比操作系统早出版了几十年。在他笔下，小鸭子 Ping 就是一个数据包，每天定时（“我怀疑是 cron 调度的”）从船（主机）出发，过桥（网桥）游进长江（互联网），被另一条船收下，兜一圈再回到原来的船上，“略显风尘”。他还夸作者没落俗套：故事发生在河上，本可以写一场“洪水”（flood ping，`ping -f`），作者却巧妙地避开了。缺点也有：没有索引，ICMP 报文结构讲得不够细。

几天后，另一位读者留言说，同事告诉她有本专讲 PING 命令的书，她就真订了一本，到手才发现是给小朋友看的绘本——不过她承认，这是她读过最好的儿童文学之一。Muuss 把这几条书评都存进了自己的主页，当作“关于另一只 ping 的故事”（[The Story of the PING Program](https://ftp.arl.army.mil/~mike/ping.html)）。

Muuss 说，他听过最好的 ping 故事是在一次 USENIX 会议上：某个网管的以太网时好时坏，他把 `ping` 的输出接到声码器上，再把声码器接进办公室的音响，音量开到耳朵受得了的最大。电脑每秒喊一声“Ping”，他满楼走，挨个去晃以太网接头，晃到哪里喊声停了，故障就在哪里。

那个晚上的仓促还留下了一条长长的尾巴。`ping` 要用原始套接字（`SOCK_RAW`），而 BSD 只允许 root 打开原始套接字，于是 `/bin/ping` 只能装成 setuid root：任何用户运行它，它都临时拥有 root 权限。一个人人都会敲的小工具，就这样在几十年里一直顶着 root 权限运行：它解析参数或回包时的任何一个 bug，都可能变成一次本地提权。发行版后来改用文件能力（只授予 `CAP_NET_RAW`）缩小了风险，但根子还在。直到 2010 年 12 月，Openwall 的 Vasiliy Kulikov 给 Linux 提交了一种新的套接字 `socket(PF_INET, SOCK_DGRAM, IPPROTO_ICMP)`，只能收发回显请求和应答，不需要任何特权，“让不带 setuid 的 /bin/ping 成为可能”（[LWN: ipv4: add ICMP socket kind](https://lwn.net/Articles/420800/)）。它在 2011 年随 Linux 3.0 合入，由 `net.ipv4.ping_group_range` 控制哪些用户组能用。补丁说明里还提了一句：Mac OS X 早就有类似的机制。Muuss 那晚一气之下写的内核支持，过了快三十年才有了一个不需要 root 的版本。

2000 年 11 月 20 日，Muuss 在 95 号州际公路的一场连环车祸中去世，年仅 42 岁（[Baltimore Sun 讣告](https://www.baltimoresun.com/2000/11/25/michael-john-muuss-42-computer-expert-whose-software-had-key-role-in-internet/)）。

早年 Phil Dykstra 给 `ping` 加过 ICMP 记录路由（Record Route）的功能，但当年很少有路由器真去处理这个 IP 选项，加上 IP 头里能记的跳数有限，这个功能几乎没用。真正把“看看包走了哪条路”做成杀手级工具的，是另一个人。

## 1988：Van Jacobson 与 traceroute——被“不发 ICMP 的 ICMP”逼出来的巧招

1988 年年底，劳伦斯伯克利实验室（LBL）的 Van Jacobson 被一个问题折磨了一整个星期：包到底跑到哪儿去了？！（原话是 "where the !?\*! are the packets going?"）12 月 20 日凌晨，他给 IETF 和端到端兴趣组的邮件列表群发了一封信，宣布做出了一个诊断工具（[Van Jacobson 的原始邮件](https://gist.github.com/thiteixeira/50cf5f9c26ca0216e4aa6d42b2440216)）：

> 它的原理是：发一个 TTL 为 1 的 UDP 包，然后听一条 ICMP“超时”消息。收到了，就打印这条消息的源地址，再把 TTL 加 1，如此反复。（跟往常一样，这个聪明主意不是我想出来的——我是在一次端到端任务组会议上听 Steve Deering 提的。）……希望这对谁有点用。祝好运。圣诞快乐。——Van

这就是 `traceroute`。Muuss 后来坦白，看到 Jacobson 拿他写的内核 ICMP 支持做出这个东西时，他“嫉妒得发狂”：“我怎么就没想到呢！”（[The Story of the PING Program](https://ftp.arl.army.mil/~mike/ping.html)）

它的巧妙全在于“借用”了 ICMP 的两个脾气：每台路由器把 TTL 减到 0 时，都会丢包并回一条 ICMP 超时（类型 11）；于是 TTL 从 1 开始一级一级往上加，就能把路径上每一跳逼出来报到。到了目的主机，包落在一个没人监听的怪端口上，主机回一条 ICMP“端口不可达”（类型 3 代码 3），探测就此结束。

下面的演示里，每点一次就发一个 TTL 大一级的探测包，看它走到第几跳被路由器“退回”一条 ICMP 超时。第三跳的路由器被设成了不回 ICMP，这正是 traceroute 里那一行 `* * *` 的来历。

```demo
demos/icmp-traceroute.html
```

但故事真正有意思的地方，是 `traceroute` 为什么用 **UDP** 而不是 `ping` 那样的 ICMP 回显。1999 年，有人在新闻组里问起这段，Van Jacobson 亲自回了帖（[comp.protocols.tcp-ip 存档](http://root.org/ip-development/news/vanj.99feb08.txt)）：

> 最早那个版本的 traceroute（从没发布过）用的是 ICMP 回显。RFC 792 里有一句话：“为避免关于消息的消息无限递归，不对 ICMP 消息再发 ICMP 消息。”测试的第一个晚上我就发现，当时一多半的路由器厂商把这句话当了真，死活不肯为一个 ICMP 回显包回“超时”。于是我改用了 UDP。——Van

```demo
demos/icmp-why-udp.html
```

这是整篇故事里最妙的一个回旋：Postel 在 1981 年为了防无限递归写下的那句“不对 ICMP 再发 ICMP”，七年后生生逼得 Jacobson 换了一种传输协议。改用 UDP 还带来一个新麻烦：ICMP 报错只带回原始包的前 8 个字节，而这 8 字节正好是一个 UDP 头的大小。为了让同时在跑的多个 `traceroute` 互不串线、每个探测包都能精确对上回来的那条 ICMP，Jacobson 把进程的 PID 塞进源端口，每发一个探测包就把目的端口加一。那个目的端口的起始值是 `32768 + 666`——32768 是客户端端口区的起点（不太可能有人在听），而 666……Jacobson 从没解释过，后人只好当它是个恶魔玩笑（[internet-history 邮件列表的考证](https://elists.isoc.org/pipermail/internet-history/2015-September/003621.html)）。

顺带一提，traceroute 只是 Jacobson 这一年的副业。同一年，他和 Mike Karels 发表了《[Congestion Avoidance and Control](https://ee.lbl.gov/papers/congavoid.pdf)》，给 TCP 加上了慢启动和拥塞避免，把 1986 年起反复发生的“拥塞崩溃”从互联网上治好了。

`traceroute` 的源码注释本身就是一篇散文。Jacobson 在里面写了那句被引用了几十年的免责声明（[原始 BSD traceroute.c](https://github.com/openbsd/src/blob/master/usr.sbin/traceroute/traceroute.c)）：

> 别拿这个当编程范例。我当时在找一个路由问题，这段代码在连续 48 小时不睡之后就这么“蹦”出来了。它居然能编译、还能跑，我自己都惊了。

注释里还记着当年网络的种种怪癖，像一本田野笔记：某一跳之后的路由器全“消失”了，是因为 MIT 的 C 网关压根不发超时消息；某台 Sun-3 主机（跑 SunOS 3.5）回 ICMP 时，错把收到的那个已经快耗尽的 TTL 当成了回复包的 TTL，结果回复在半路又超时死掉，害得你必须用两倍于实际跳数的 TTL 才能探到它——“一个 TTL 为 1 回来的包，就是这个毛病的信号”。`traceroute` 会在这种时间后面打一个 `!`。这些注释提醒我们：ICMP 的理想很干净，现实里每家实现都有自己的脾气。

## 1988–1990：Path MTU Discovery——让路由器回一句“包太大”

ICMP 的第三个大用途，藏在类型 3 里一个不起眼的代码里。

互联网是由各种 MTU（最大传输单元）不一的链路拼起来的：以太网能过 1500 字节，有的隧道只能过 1480 甚至更小。一个大包撞上一段更窄的链路，怎么办？早年的答案是分片，但分片代价很大（这是另一篇文章的主题，见[《MTU：1500 字节怎样成了互联网改不动的轨距》](/blog/mtu/)）。更聪明的办法，是让发送方**事先知道**整条路上最窄的那一截有多宽，从一开始就不发会被卡住的包。

1988 年的 [RFC 1063](https://www.rfc-editor.org/rfc/rfc1063) 试过一个方案：定义两个新的 IP 选项，让沿途每个路由器把自己的出口 MTU 填进去。可它要求路径上每一台路由器都认识这个新选项，推不动。1990 年，DEC 的 Jeffrey Mogul 和斯坦福的 Steve Deering（就是给 Jacobson 出 traceroute 主意的那位）合写了 [RFC 1191](https://www.rfc-editor.org/rfc/rfc1191)，换了思路，而且这个思路完全押在 ICMP 身上：发送方给所有包打上“不许分片（DF）”标志；某个路由器发现包太大又不许切，就丢掉它，回一条 ICMP“目的不可达——需要分片但设了 DF”（类型 3，代码 4）；RFC 1191 还把这条消息里原本“未用、必须为 0”的 16 位，重新定义成“下一跳的 MTU”，直接告诉发送方该缩到多大。

这就是 Path MTU Discovery（路径 MTU 发现，PMTUD）。它的全部前提只有一句话：**那条 ICMP“包太大”，必须能一路回到发送方手里**。

记住这句话。整个 ICMP 故事的后半段，几乎所有的麻烦，都出在这句话被破坏上。而破坏它的，恰恰是人们为了“安全”而做的事。

## 1996：死亡之 Ping——报信者第一次被当成凶器

1996 年 10 月 21 日，伦敦的 Mike Bremford 开了一个网页，标题是“死亡之 Ping 页面！教你怎样搞崩自己的操作系统！”（[Ping of Death 页面存档](https://insecure.org/sploits/ping-o-death.html)，后来由 Malachi Kenney 接手维护）。原理简单得离谱：IP 包头里的总长度字段只有 16 位，一个 IP 包最大 65535 字节。可分片机制允许最后一片的偏移量加上长度超过这个数。Windows 95 和 NT 的 `ping` 恰好不拦着你：`ping -l 65510` 让它带 65510 字节数据，加上 8 字节 ICMP 头、20 字节 IP 头，一共 65538 字节，被切成几十片发出去。接收方把这些分片拼起来时，内核里那些按 16 位算的缓冲区就溢出了，机器当场崩溃、重启或死机。

最要命的是门槛：攻击者只需要知道对方的 IP，连一行代码都不用写。Bremford 写道，测试时他在伦敦的机器被伯克利的一台机器“ping 死”过。三个月里，18 种以上的主流操作系统被确认中招：Windows、各种 Unix、Mac、Netware、打印机、路由器……没有 Windows 的人也不用着急，Bill Fenner 很快贴出了一个能在 Unix 上模拟这一招的 C 程序。补丁发得飞快，页面上甚至搞了个“颁奖”：Linux 社区从漏洞公开到补丁发布只用了 2 小时 35 分 10 秒——不过 Telebit 的 Bill Webb 坚称自家 Netblazer 路由器的补丁两小时内就出了，“好吧好吧，奖金你们分”。

但这次事件真正的遗产，是一句错误的经验总结。页面里专门强调过一段警告（[Ping of Death 页面存档](https://insecure.org/sploits/ping-o-death.html)）：

> 说了这么多 ping，很容易忽略问题的根子。这个漏洞绝不限于 ping——任何能发出 IP 数据报的东西都能利用它，TCP、UDP 都行。所以在防火墙上挡 ping 只是临时手段。唯一的解决办法是修内核里重组分片的代码。别以为挡了 ping 就安全了。

这段话写得很清楚：**问题在分片重组的内核代码，挡 ping 没用**。可绝大多数管理员只记住了前半句的那个动作——“挡 ping”——没记住后半句的结论。从死亡之 Ping 开始，“在防火墙上把 ICMP 禁了”第一次被当成了一种防御姿势。

## 1996：Loki——ICMP 里还能藏私货

几乎在死亡之 Ping 的同时，另一件事进一步败坏了 ICMP 在管理员心里的名声。

1996 年，黑客杂志《Phrack》第 49 期登了一篇叫《Project Loki》的白皮书，标题下写着“August 1996”：文章出自 daemon9（又名 route），源码由他和 alhambra 合写（[Phrack 49, File 6](http://phrack.org/issues/49/6.html)）。它指出了一个此前没人正眼看过的事实：ICMP 回显包的数据区可以装任意内容，而网络设备从不检查这段内容的含义——它们只负责转发、丢弃或原样返回。于是，只要一个网络允许 ping 通过，就存在一条隐蔽信道：把要偷传的数据（比如一台被攻陷主机上的 shell 命令和输出）伪装成普普通通的 ping 流量，大摇大摆地穿过防火墙。

> 如果 ICMP 回显流量被放行，这条信道就存在。只要这条信道存在，它就是一个无法被击败的后门……即便部署了严密的防火墙和包过滤，这条信道依然存在（前提当然是它们没有禁掉 ICMP 回显）。

次年的《Phrack》第 51 期放出了完整实现 LOKI2（[Phrack 51, File 6](http://phrack.org/issues/51/6.html)），还支持加密和 ICMP/DNS 两种载体。Loki 本身是一种“已被攻陷之后”的后门，并不能帮攻击者攻进来；但它传递的信息很清楚：ICMP 不只是无害的诊断工具，它还能当走私通道。对一个安全管理员来说，这又是一条“不如干脆禁掉”的理由。

## 1997–1999：Smurf——把一个 T1 放大成一场海啸

真正把“屏蔽 ICMP”从个别管理员的偏方，变成全行业默认操作的，是 Smurf。

1997 年 10 月，一个化名 TFreak（真名 Dan Moschuk）的人在 Bugtraq 邮件列表上公开了一个叫 `smurf.c` 的小程序（[Bugtraq 存档](https://seclists.org/bugtraq/1997/Oct/66)）。它的手法简单而歹毒：向一个网络的**广播地址**发 ICMP 回显请求，并把源地址伪造成受害者的 IP。那个网络里所有主机都会对这个广播请求做出回显应答，而应答全都涌向那个被伪造的受害者。一个请求，换来几十上百个应答。

Craig Huegen 写过一份流传很广的白皮书，算过这笔账（[The Latest in Denial of Service Attacks: Smurfing](https://web.archive.org/web/20121107175456/www.pentics.net/denial-of-service/white-papers/smurf.cgi)）：假设攻击者有一条 T1 线路，往一个有 100 台主机的网络广播地址发 768 kb/s 的 ping，经过放大，受害者会收到 76.8 Mbps 的洪水。那些无辜的中间网络被称作“放大器（amplifier）”，受害者则被这股被放大了几十倍的流量彻底冲垮。当年挨打最多的，是 IRC 服务器。

下面的演示把这个过程拆成几步：攻击者只发一个包，路由器把它变成整个网段的广播，所有主机一起朝受害者“回声”。最后一步打开 `no ip directed-broadcast`，看放大器怎样被拆掉。

```demo
demos/icmp-smurf.html
```

最耐人寻味的是 TFreak 公开代码时写的那段话（[Bugtraq 存档](https://cliplab.org/~alopez/bugs/bugtraq3/0070.html)）：

> 先说清楚，这段代码是个错误。写它的时候我没想到：第一，全世界都会拿到它；第二，它对那些被用来放大的机器会有这么大的破坏力。是我无知。我极度后悔写了它，但你我都知道，东西不被“玩坏”，就不会有人去修它。

接着他讲怎样防。被打的一方，他说自己也不知道有什么好办法，除非在路由器上拒掉所有进来的 ICMP，“但这不是好办法，那样 ping 和 traceroute 这些有用的小玩意儿就都用不了了”。不想被当成放大器的一方倒很简单：在路由器上过滤掉发往广播地址的 ICMP，一行配置就够。连写攻击程序的人都知道“全禁 ICMP”是过度反应，可现实里，被 Smurf 打怕了的网络，很多就是这么干的。

1998 年 1 月，CERT 发布了公告 [CA-98.01](https://seclists.org/bugtraq/1998/Jan/4)，正式记录 Smurf。1999 年 8 月，IETF 发表 [RFC 2644](https://www.rfc-editor.org/rfc/rfc2644)，把路由器的一个默认行为彻底改了：在此之前，标准要求路由器默认转发“定向广播”；从此以后，默认不转发。思科也在 IOS 12.0 里把 `no ip directed-broadcast` 设成了默认。釜底抽薪地堵上了放大器。TFreak 后来还写了个用 UDP 回显做同样事情的变种，叫 fraggle——顺带把 UDP 的 echo 服务也拖下了水。

到这里，经过死亡之 Ping、Loki、Smurf 三连击，一条根深蒂固的信条在运维圈里立住了：**ICMP 是危险的，能禁就禁**。它看起来还很安全：ping 不通，扫描的人就摸不到我。

## 2000–2012：屏蔽成为默认，ICMP 被一项项废掉

进入 2000 年代，屏蔽 ICMP 从“高手的操作”变成了“出厂设置”。2004 年，Windows XP Service Pack 2 把 Windows 防火墙默认打开，默认丢弃一切未经请求的入站流量——包括 ICMP 回显请求。于是一台刚装好的 XP，默认就是 ping 不通的；想让它能被 ping，得手动勾上“允许传入回显请求”（[微软 The Cable Guy 专栏](https://learn.microsoft.com/en-us/previous-versions/bb877964(v=technet.10))）。无数家庭和公司网络，从此默认对 ping 装聋作哑。

“禁还是不禁”从此成了网络圈吵不完的一架。主张屏蔽的一方理由很直观：不回 ping，扫描器就摸不清哪些地址上有活机器；少开一扇门，总归少一分风险。反对的一方先搬出标准：1989 年的主机要求规范 [RFC 1122](https://www.rfc-editor.org/rfc/rfc1122#section-3.2.2.6) 白纸黑字写着，每一台主机都**必须**实现 ICMP 回显服务器，收到回显请求就回应答。然后是实际效果：这是典型的“靠藏起来求安全”，一台 Web 服务器本来就在 80 端口上听着，不回 ping 又能藏住什么？有人干脆建了个网站，名字就叫 [Should I block ICMP?](http://shouldiblockicmp.com/)，逐条列出哪些 ICMP 万万不能挡，写到“包太大”时连用两个“重要”，第二个还是全大写的 VERY。对于挡掉超时消息、让 traceroute 中间全是星号的管理员，它只有一句：“别当那种人，好吗？”网站自己也承认，这个话题“总是以困惑、愤怒和近乎狂热的分歧收场”。

与此同时，ICMP 自己也在“瘦身”，一些早年的消息类型被陆续判了死刑：

- **源抑制（类型 4）**，那个让发送方“发慢点”的消息，被发现既无效又不公平——它惩罚的往往是老实人。早在 1995 年的 [RFC 1812](https://www.rfc-editor.org/rfc/rfc1812) 就建议路由器别再发了；2012 年的 [RFC 6633](https://www.rfc-editor.org/rfc/rfc6633) 彻底终结它：主机**不得**发送，TCP 收到后**必须**默默丢弃，路由器**必须**忽略。拥塞控制这件事，早已完全交给了 TCP 自己。IPv6 的 ICMP 干脆从一开始就没定义源抑制。
- **信息请求/应答（15/16）、地址掩码（17/18）、以及一个从没真正部署过的 ICMP Traceroute（类型 30）** 等一堆老古董，在 2013 年的 [RFC 6918](https://www.rfc-editor.org/rfc/rfc6918) 里被集体正式废弃，IANA 的注册表里一个个标上了“Deprecated”。

更深一层的不信任，来自一类针对 TCP 的攻击。2004 到 2005 年，英国 NISCC 协调披露了一组漏洞，Fernando Gont 后来把它们写进了 [RFC 5927](https://www.rfc-editor.org/rfc/rfc5927)《ICMP Attacks against TCP》。问题的根子，正是 ICMP 报错里带回的那“原始包前 8 字节”：一个在网络外面、看不到你任何流量的攻击者，只要能猜中一条 TCP 连接的四元组，就能伪造一条 ICMP“硬错误”（比如目的不可达），让受害的 TCP 以为这条连接彻底坏了，直接把它掐断——这叫“盲连接重置”。类似地，伪造一条把“下一跳 MTU”填成 68 字节的类型 3 代码 4，就能把一条连接的性能拖到谷底。从那以后，各家 TCP/IP 栈学会了对收到的 ICMP 报错做严格校验，不再见到就信。

这是一种悖论式的处境：RFC 792 白纸黑字写着“ICMP 是 IP 不可分割的一部分”，现实里它却被当成外人——默认屏蔽，逐条废弃，收到也不敢全信。可就在人们以为能安全地把它关在门外时，它用一种最难查的方式证明了自己确实“不可分割”。

## 2000：ICMP 黑洞——ping 得通，网页却打不开

还记得 PMTUD 那句唯一的前提吗？——路由器回的那条 ICMP“包太大（类型 3 代码 4）”，必须能回到发送方。

当一个防火墙“把 ICMP 全禁了”的时候，它禁掉的不只是 ping 用的回显请求，还有和回显一起被一刀切掉的那条“包太大”。于是 PMTUD 瞎了：发送方不停地发大包，路径上的瓶颈路由器不停地丢、不停地回“包太大”，而这些消息全被某处的防火墙吃掉，发送方永远收不到，永远不知道该把包改小。它的包，就这样一个接一个消失在一个黑洞里。

2000 年 9 月，Kevin Lahey 在 [RFC 2923](https://www.rfc-editor.org/rfc/rfc2923) 里给这种故障起了个正式的名字——**PMTUD 黑洞**，并点出了它最折磨人的地方：

> 这种故障特别难调试，因为 ping 和一些交互式的 TCP 连接都能通。批量传输在遇到第一个大包时就失败，连接最终超时。

下面的演示是一条典型的坏路径：服务器在防火墙后面，中间有一段 1492 字节的 PPPoE 链路。先发一个小包，再发一个满载的大包，然后把防火墙切成放行 ICMP，看同一条路径怎样从黑洞变回正常。

```demo
demos/icmp-blackhole.html
```

这正是开头说的那种症状：`ping` 能通，SSH 能登上，可一传大文件就卡死，网页只加载出一半，`git clone` 停在某个百分比，HTTPS 在服务器发证书那一刻挂住（证书链往往是握手里第一个满载的大包）。小包畅通无阻，大包凭空蒸发。Lahey 还点出了一个更荒诞的后果：同一条坏掉的路径，老老实实实现了 PMTUD 的 TCP 反而不通，而那些偷懒、让路由器帮忙分片的旧实现却能工作——于是认真遵守标准的人被用户骂。关于 1500 字节、PPPoE 的 1492、各种隧道黑洞的完整故事，都在[《MTU》](/blog/mtu/)那篇里。

既然没法让全世界的防火墙都改配置，运营商和路由器厂商就想了一个绕路的办法：**MSS 钳制**。TCP 建连接时，双方会在 SYN 包里各自报一个“最大报文段长度”（MSS），告诉对方“你发给我的每段数据别超过这么多”。家用路由器站在 PPPoE 链路的门口，看到经过的 SYN 包，就悄悄把里面的 MSS 改小，比如改成 1452（1492 减去 40 字节的 IP 和 TCP 头）。这样两端从一开始就不会发出过大的包，PMTUD 能不能用也就无所谓了。Linux 里这是一条 `iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`，几乎每台家用路由器的固件里都有它。这是一种很能说明问题的修法：中间设备偷偷改写别人的 TCP 头，只为了绕开另一些中间设备丢掉的 ICMP。它只管得了 TCP，UDP 上的协议照样掉进黑洞。

黑洞也不全是防火墙造的。2015 年 2 月，Cloudflare 的 Marek Majkowski 写了一篇事故复盘（[Path MTU discovery in practice](https://blog.cloudflare.com/path-mtu-discovery-in-practice/)）：一小批通过 IPv6 隧道上网的用户突然打不开 Cloudflare 的网站了。原因是他们在数据中心里扩大了 ECMP（等价多路径）负载均衡的使用范围。路由器按 TCP 的四元组做哈希，保证同一条连接的包总落到同一台服务器；可它不认识 ICMP，只按源地址和目的地址哈希。而“包太大”的源地址是互联网上某台路由器，于是这条 ICMP 经常被送到另一台根本不认识这条连接的服务器上，真正需要它的那台永远收不到。临时办法是把所有 IPv6 路径的 MTU 压到 1280，IPv4 则打开了 RFC 4821 的探测；最终他们写了个小守护进程 [pmtud](https://github.com/cloudflare/pmtud)，把收到的每一条“包太大”广播给机房里所有服务器。没有任何人想挡 ICMP，ICMP 还是在半路走丢了：现代网络里的大部分设备，从设计上就没把这个报信者当成连接的一部分。

ICMP 用这种方式报了一箭之仇：你可以不欢迎报信者，但你删不掉他要报的那件事。你只是把“包太大”这个事实，从一句清楚的话，变成了一次次沉默的消失。

## 1995–2020：ICMPv6——把被嫌弃的信使扶上正位

1990 年代中期设计 IPv6 的时候，设计者们吸取了很多教训，其中一条就是重新想清楚 ICMP 该是什么地位。结果是一次彻底的翻盘。

ICMPv6 由 Alex Conta 和 Steve Deering 起草，1995 年底首次发布为 [RFC 1885](https://www.rfc-editor.org/rfc/rfc1885)，1998 年修订，2006 年的 [RFC 4443](https://www.rfc-editor.org/rfc/rfc4443) 是今天的版本。它在 IPv6 包头里的“下一个头部”编号是 58，规范里写得毫不含糊：

> ICMPv6 是 IPv6 不可分割的一部分，每一个 IPv6 节点都**必须**完整实现本规范要求的全部消息和行为。

这句话和 RFC 792 那句几乎一字不差，但这一次它是认真的——因为 IPv6 把一堆核心功能直接架在了 ICMPv6 上，没有它网络根本转不起来：

- **邻居发现（Neighbor Discovery，NDP）**，定义在 [RFC 4861](https://www.rfc-editor.org/rfc/rfc4861)，一口气取代了 IPv4 里的三样东西：把 IP 解析成 MAC 地址的 ARP、发现路由器的 Router Discovery、以及 ICMP 重定向。你的机器怎么找到同网段的邻居、怎么知道默认网关是谁、怎么自动配上地址——全靠一组 ICMPv6 消息（邻居请求/通告、路由器请求/通告、重定向）。
- **“包太大”（类型 2）** 成了绝对不能挡的消息。因为 IPv6 的路由器**不再做分片**，PMTUD 从“建议”变成了“必需”——发送方要是收不到“包太大”，就只能一直往黑洞里倒包。

换句话说，在 IPv6 的世界里，你**没法**像对待 IPv4 那样简单粗暴地“把 ICMP 全禁了”——禁了，连邻居都找不到，地址都配不上，网就直接断了。IETF 为此专门写了 [RFC 4890](https://www.rfc-editor.org/rfc/rfc4890)，逐条说明哪些 ICMPv6 消息绝对不能过滤，“包太大”和邻居发现相关的消息排在最前面。那个在 IPv4 时代被赶出门的信使，到了 IPv6，成了没有就进不了门的管家。

但身份的抬升也意味着攻击面的扩大。ICMPv6 越重要，拿它做文章的攻击也越花样百出：

- 2011 年 4 月，安全研究者 Marc Heuse 公开了一个问题：往局域网里猛发随机的伪造路由器通告（Router Advertisement），收到的机器会手忙脚乱地更新路由表、给自己配新地址，直到 CPU 耗尽。思科的设备、Juniper 的 Netscreen、FreeBSD、所有版本的 Windows 都中招，Windows 上是 CPU 和内存双双 100%，个人防火墙也挡不住（[Full Disclosure 存档](https://seclists.org/fulldisclosure/2011/Apr/86)）。他的工具包 thc-ipv6 里一个 `flood_router6` 就能复现。公告第一行写着发布的理由：思科三个月内就修好了，微软在 2010 年 8 月确认了漏洞，却表示不打算修。几年后，微软在 [MS14-006](https://learn.microsoft.com/en-us/security-updates/securitybulletins/2014/ms14-006) 里为 Windows 8 和 Server 2012 修了一个同类的路由器通告洪水问题，给的临时缓解办法之一，正是关掉入站的路由器通告——也就是让 IPv6 自动配置失灵。
- 2020 年 10 月，微软修复了 [CVE-2020-16898](https://blog.quarkslab.com/beware-the-bad-neighbor-analysis-and-poc-of-the-windows-ipv6-router-advertisement-vulnerability-cve-2020-16898.html)，绰号“坏邻居（Bad Neighbor）”。一个精心构造的 ICMPv6 路由器通告，在其中的 RDNSS（递归 DNS 服务器）选项里把长度字段填成一个偶数，就能让 Windows 的 `tcpip.sys` 在解析时算错偏移、栈溢出，配合 IPv6 分片甚至可能远程执行内核代码。同样是一个畸形的 ICMP 包、同样要靠分片、同样打崩内核，Rapid7 的分析文章干脆把标题写成了“[Ping of Death Redux](https://www.rapid7.com/blog/post/ra-cve-2020-16898-aka-bad-neighbor-ping-of-death-redux-analysis/)”：死亡之 Ping，二十四年后重演。
- 还有更隐蔽的，而且不分 IPv4 和 IPv6。2020 年的 SAD DNS 攻击（UC Riverside 与清华的研究，[论文](https://www.cs.ucr.edu/~zhiyunq/pub/ccs20_dns_poisoning.pdf)）盯上的是 Linux 为了防滥发而加的**全局 ICMP 限速**——它是一个所有目标共享的计数器。攻击者通过观察“限速额度有没有被耗掉”这个侧信道，就能推断出 DNS 查询用的是哪个随机端口，把本已被认为死掉的 DNS 缓存投毒攻击重新救活。一个为了遏制 ICMP 滥用而加的防御机制，自己变成了泄密的缝隙。

ICMP 的身世在这里绕了一个完整的圈：它生来就被说成是 IP 不可分割的一部分，中途被整个行业当外人屏蔽、裁撤、提防，到了 IPv6 又被重新郑重地写回“不可分割”——然后立刻又成了新的攻击面。报信者的命运，似乎永远是这样：你需要他，又提防他。

## 2007–2020：PLPMTUD——干脆不再相信 ICMP

故事的最后一程，是人们对 ICMP 的信任彻底幻灭之后做出的选择：既然这条“包太大”靠不住，那就不靠它。

2007 年，Matt Mathis 和 John Heffner 发表 [RFC 4821](https://www.rfc-editor.org/rfc/rfc4821)，提出 PLPMTUD（分包层路径 MTU 发现）。思路是让 TCP 自己探测：先用一个肯定安全的小包长稳稳地发，时不时插一个更大的探测包；探测包被确认了，说明路径能过这么大，就把包长往上提；探测包丢了、而紧跟着的正常小包没丢，说明是包太大而不是网络拥塞，就退回到安全值。整个过程**一条 ICMP 都不需要**。Linux 把它做成了开关 `net.ipv4.tcp_mtu_probing`。

2020 年的 [RFC 8899](https://www.rfc-editor.org/rfc/rfc8899) 把同样的思路推广到 UDP 上的协议，QUIC 是最大的用户：[RFC 9000](https://www.rfc-editor.org/rfc/rfc9000#section-14) 规定 QUIC 从一个保守的小包长起步，想用更大的包就自己探测，绝不依赖路径上任何人来报信。

从这里回头看，PMTUD 走了一个整圈。1990 年的 RFC 1191 选择让网络主动告诉主机“路有多宽”——这是对 ICMP 的信任。三十年后，主机们得出的结论是：网络的话不能全信，还是自己一个包一个包地试更可靠。ICMP 最经典的那个用途，就这样被它自己培养出的不信任，温柔地架空了。

## 2007–2021：信使还在学新本事

被架空的同时，ICMP 自己也没停止长大，只是动静小得多。

2007 年 4 月的 [RFC 4884](https://www.rfc-editor.org/rfc/rfc4884) 给超时、目的不可达等几类报错加了一个可扩展的尾巴：带回的原始包片段补齐到 128 字节，后面再挂若干个结构化的“扩展对象”。第一个用上它的是同年 8 月的 [RFC 4950](https://www.rfc-editor.org/rfc/rfc4950)：运营商骨干网里大量跑着 MPLS，路由器回超时消息时可以把包当时带着的 MPLS 标签栈一起附上。今天你在 traceroute 里看到某一跳后面跟着 `[MPLS: Label 24005 Exp 0]`，就是这个扩展。2010 年的 [RFC 5837](https://www.rfc-editor.org/rfc/rfc5837) 又允许路由器附上收到包的那个接口的名字、编号和 MTU，traceroute 从此不光知道“经过了哪台路由器”，还能知道“从它的哪个口进的”。

2018 年 2 月，Juniper、Google 等公司的几位工程师发表了 [RFC 8335](https://www.rfc-editor.org/rfc/rfc8335)，定义了一种叫 PROBE 的扩展回显（类型 42/43）。普通的 ping 只能问“你这个地址还活着吗”；PROBE 可以通过一个能 ping 通的地址，去问它身上另一个接口的状态，哪怕那个接口没有可路由的地址，比如只配了 IPv6 链路本地地址、或者是一条没编号的点对点链路。Linux 在 2021 年的 5.13 版内核里加了对它的支持，开关是 `net.ipv4.icmp_echo_enable_probe`。

从 1972 年那声 ECO 算起，回显请求这个最古老的动作，五十年后还在长出新的变种。

## 尾声：报信者的宿命

把 ICMP 的一生摆在一起看，是一条奇怪的曲线。

它出身名门——RFC 792 第一句就说它是 IP 不可分割的一部分。它催生了两个几乎每个人都用过的工具，`ping` 和 `traceroute`，各自带着一夜写成、48 小时没睡的传奇。它默默撑起了 PMTUD，让大包能在参差不齐的链路间找到自己的尺寸。

可它也一次次被当成凶器：死亡之 Ping 用它崩溃内核，Loki 用它走私数据，Smurf 用它放大出海啸。于是它被整个行业钉上了“危险”的标签，在防火墙上被一刀切掉，在标准里被一条条废弃，连收到都不敢全信。而这份屏蔽，又亲手造出了 ICMP 黑洞。

到了 IPv6，钟摆荡了回来：ICMPv6 被扶正成管家，禁了就上不了网——然后它立刻又成了“坏邻居”和 SAD DNS 的新战场。与此同时，最依赖它的 PMTUD，正在学着不再依赖它。

这大概就是一个报信者的宿命。他带来的从不是好消息——目的不可达、超时、包太大、你发慢点——没有人喜欢坏消息，于是没有人真心欢迎报信者。可网络和现实世界一样：你可以捂住信使的嘴，但那件要报的坏事，一件都不会因此消失。它只会从一句听得懂的话，变成一个你查了三天也想不明白的黑洞。

## 参考

- Mike Muuss，[The Story of the PING Program](https://ftp.arl.army.mil/~mike/ping.html)；[Michael John Muuss 主页](https://ftp.arl.army.mil/mike/)。
- Baltimore Sun，[Michael John Muuss, 42, computer expert whose software had key role in Internet](https://www.baltimoresun.com/2000/11/25/michael-john-muuss-42-computer-expert-whose-software-had-key-role-in-internet/)，2000-11-25。
- A. McKenzie、S. Crocker，[RFC 6529: Host/Host Protocol for the ARPA Network](https://www.rfc-editor.org/rfc/rfc6529)（NIC 8246 的存档，含 ECO/ERP/ERR）；[RFC 695: Official change in Host-Host Protocol](https://www.rfc-editor.org/info/rfc695/)。
- Virginia Strazisar，[IEN 109: How to Build a Gateway](https://www.rfc-editor.org/ien/ien109.txt)，1979-08。网关类型字段与 ICMP 类型号的由来。
- Jon Postel，[RFC 777: Internet Control Message Protocol](https://www.rfc-editor.org/rfc/rfc777)，1981-04；[RFC 792](https://www.rfc-editor.org/rfc/rfc792)，1981-09（STD 5）；[RFC 791: Internet Protocol](https://www.rfc-editor.org/rfc/rfc791)，1981-09。
- Van Jacobson，[4BSD routing diagnostic tool available for ftp](https://gist.github.com/thiteixeira/50cf5f9c26ca0216e4aa6d42b2440216)，1988-12-20；[Re: traceroute history: why UDP?](http://root.org/ip-development/news/vanj.99feb08.txt)，1999；[原始 traceroute.c 源码注释](https://github.com/openbsd/src/blob/master/usr.sbin/traceroute/traceroute.c)；[关于 33434 = 32768 + 666 的考证](https://elists.isoc.org/pipermail/internet-history/2015-September/003621.html)；Van Jacobson、Michael Karels，[Congestion Avoidance and Control](https://ee.lbl.gov/papers/congavoid.pdf)，SIGCOMM 1988。
- [RFC 1063](https://www.rfc-editor.org/rfc/rfc1063)（IP MTU Discovery Options，1988）；[RFC 1191](https://www.rfc-editor.org/rfc/rfc1191)（Path MTU Discovery，1990）；[RFC 2923](https://www.rfc-editor.org/rfc/rfc2923)（TCP Problems with PMTUD，命名 PMTUD 黑洞，2000）。
- [RFC 1122: Requirements for Internet Hosts](https://www.rfc-editor.org/rfc/rfc1122#section-3.2.2.6)，1989（每台主机必须实现回显服务器）。
- Vasiliy Kulikov，[ipv4: add ICMP socket kind](https://lwn.net/Articles/420800/)，2010-12（无特权 ping 套接字）。
- Mike Bremford 创建、Malachi Kenney 维护，[Ping of Death 页面存档](https://insecure.org/sploits/ping-o-death.html)（含 Bill Fenner 的 win95ping.c），1996-10。
- daemon9 / route，[Project Loki](http://phrack.org/issues/49/6.html)，Phrack 49，1996（[纯文本存档](https://www.securiteinfo.com/archives/rare-and-old-hacking/p49-06.txt)）；LOKI2，[Phrack 51](http://phrack.org/issues/51/6.html)，1997（[纯文本存档](https://www.opennet.ru/base/sec/p51-06.txt.html)）。
- TFreak (Dan Moschuk)，[`smurf' multi-broadcast icmp attack](https://seclists.org/bugtraq/1997/Oct/66)（含作者悔过原文的[另一份存档](https://cliplab.org/~alopez/bugs/bugtraq3/0070.html)），1997-10；Craig A. Huegen，[The Latest in Denial of Service Attacks: Smurfing](https://web.archive.org/web/20121107175456/www.pentics.net/denial-of-service/white-papers/smurf.cgi)；CERT，[CA-98.01: smurf](https://seclists.org/bugtraq/1998/Jan/4)，1998-01；[RFC 2644: Changing the Default for Directed Broadcasts in Routers](https://www.rfc-editor.org/rfc/rfc2644)，1999-08。
- Microsoft，[The Cable Guy: Windows Firewall in XP SP2](https://learn.microsoft.com/en-us/previous-versions/bb877964(v=technet.10))，2004。
- [Should I block ICMP?](http://shouldiblockicmp.com/)（逐条说明哪些 ICMP 不该挡）。
- Marek Majkowski，[Path MTU discovery in practice](https://blog.cloudflare.com/path-mtu-discovery-in-practice/)，Cloudflare，2015-02；[cloudflare/pmtud](https://github.com/cloudflare/pmtud)。
- [RFC 1812: Requirements for IP Version 4 Routers](https://www.rfc-editor.org/rfc/rfc1812)，1995；[RFC 6633: Deprecation of ICMP Source Quench Messages](https://www.rfc-editor.org/rfc/rfc6633)，2012；[RFC 6918: Formally Deprecating Some ICMPv4 Message Types](https://www.rfc-editor.org/rfc/rfc6918)，2013。
- Fernando Gont，[RFC 5927: ICMP Attacks against TCP](https://www.rfc-editor.org/rfc/rfc5927)，2010；US-CERT，[VU#222750](https://www.kb.cert.org/vuls/id/222750)。
- [RFC 1885: ICMPv6 初版](https://www.rfc-editor.org/rfc/rfc1885)，1995；[RFC 4443: ICMPv6](https://www.rfc-editor.org/rfc/rfc4443)，2006；[RFC 4861: Neighbor Discovery for IPv6](https://www.rfc-editor.org/rfc/rfc4861)，2007；[RFC 4890: Recommendations for Filtering ICMPv6](https://www.rfc-editor.org/rfc/rfc4890)，2007。
- Marc Heuse，[ICMPv6 Router Announcement flooding DoS（CVE-2010-4669 等）](https://seclists.org/fulldisclosure/2011/Apr/86)，2011；Microsoft，[MS14-006](https://learn.microsoft.com/en-us/security-updates/securitybulletins/2014/ms14-006)，2014；Quarkslab，[Beware the Bad Neighbor（CVE-2020-16898）](https://blog.quarkslab.com/beware-the-bad-neighbor-analysis-and-poc-of-the-windows-ipv6-router-advertisement-vulnerability-cve-2020-16898.html)，2020；Rapid7，[CVE-2020-16898 aka Bad Neighbor / Ping of Death Redux](https://www.rapid7.com/blog/post/ra-cve-2020-16898-aka-bad-neighbor-ping-of-death-redux-analysis/)，2020。
- Keyu Man 等，[DNS Cache Poisoning Attack Reloaded: Revolutions with Side Channels](https://www.cs.ucr.edu/~zhiyunq/pub/ccs20_dns_poisoning.pdf)（SAD DNS，ICMP 全局限速侧信道），CCS 2020。
- Matt Mathis、John Heffner，[RFC 4821: Packetization Layer Path MTU Discovery](https://www.rfc-editor.org/rfc/rfc4821)，2007；[RFC 8899: DPLPMTUD](https://www.rfc-editor.org/rfc/rfc8899)，2020；[RFC 9000 §14: QUIC](https://www.rfc-editor.org/rfc/rfc9000#section-14)，2021。
- [RFC 4884: Extended ICMP to Support Multi-Part Messages](https://www.rfc-editor.org/rfc/rfc4884)，2007；[RFC 4950: ICMP Extensions for MPLS](https://www.rfc-editor.org/rfc/rfc4950)，2007；[RFC 5837: Extending ICMP for Interface and Next-Hop Identification](https://www.rfc-editor.org/rfc/rfc5837)，2010；[RFC 8335: PROBE](https://www.rfc-editor.org/rfc/rfc8335)，2018。
