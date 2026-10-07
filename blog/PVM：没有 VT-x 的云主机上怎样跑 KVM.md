# PVM：没有 VT-x 的云主机上怎样跑 KVM

CubeSandbox 的部署文档里有一份单独的“PVM 部署”。它面对的情况很常见：买来的云服务器上没有 `/dev/kvm`，云厂商没有开放嵌套虚拟化，微虚拟机沙箱就起不来。装上一个 PVM 宿主机内核、`modprobe kvm_pvm` 之后，`/dev/kvm` 出现了，沙箱照常运行。官方在腾讯云标准型 CVM 上测得单并发创建 67 毫秒左右。

CPU 没给虚拟化扩展，KVM 是怎么跑起来的，又为什么不慢？这篇报告依据 SOSP 2023 的论文《PVM: Efficient Shadow Paging for Deploying Secure Containers in Cloud-native Environments》、2024 年 2 月发到内核邮件列表的 73 个 RFC 补丁和其中的 PVM 规范，以及 CubeSandbox 仓库里的部署脚本和 VMM 代码，把这件事讲清楚。

PVM 是 Pagetable-based Virtual Machine 的缩写，出自阿里云和蚂蚁集团。论文的两位共同第一作者是 Hang Huang 和 Lai Jiangshan，后者也是内核补丁的作者。腾讯云在此基础上做了改进，放进了 OpenCloudOS 内核，CubeSandbox 用的就是这一版。

## 嵌套虚拟化慢在哪里

先约定层次：L0 是云厂商的 hypervisor，跑在物理机上；L1 是我们租到的云主机；L2 是在云主机里再起的沙箱虚拟机。

Intel VT-x 和 AMD-V 只提供一层硬件虚拟化。要让 L1 也能用 VMX 起 L2，L0 只能在软件里模拟一套 VMX 给 L1 用。于是 L2 每次退出都会先落到 L0，因为硬件只认 L0；L0 再把这次退出转交给 L1。L1 处理完要恢复 L2，执行的 VMRESUME 本身又是特权指令，再陷入 L0 一次。单层虚拟化里一次退出就完事的操作，到了嵌套里至少翻倍。论文测得，嵌套下 L2 到 L1 的一次切换要 1.3 微秒，单层虚拟化只要 0.105 微秒，差了一个数量级。

真正拖垮性能的是内存。硬件 MMU 只有两级地址翻译：客户机页表加一张 EPT。嵌套却有三级：L2 虚拟地址到 L2 物理地址，再到 L1 物理地址，再到宿主机物理地址。KVM 默认的做法叫 EPT-on-EPT：L1 维护一张 EPT12，L0 把它和自己的 EPT01 合成一张 EPT02 交给硬件。为了知道 EPT12 何时变了，L0 把它设成对 L1 只读，L1 每改一项都要陷入 L0。论文算过：L2 碰到一块新内存，用 4 级页表时要 14 次世界切换、7 次陷入 L0。容器一多，这些合成和同步工作全压在 L0 身上，并发越高越糟。

成本之外还有门槛。开放嵌套虚拟化会让 L0 变复杂、攻击面变大，带着 L2 的 L1 也很难热迁移，所以不少云厂商干脆不开，开了的也常有限制。蚂蚁的团队在 RFC 里写得直白：出于安全考虑，L0 关掉了嵌套虚拟化，我们没法直接用 KVM。

## PVM 的思路

PVM 换了个问题：沙箱要的只是一层强隔离，不需要一台能跑任意 hypervisor 的完整虚拟机。既然这样，就不让 L2 用硬件虚拟化，而是在 L1 里用软件把它包起来。对 L0 来说，L1 自始至终是一台普通虚拟机。

做法分三块。第一，把 L2 的用户态和内核都放到 L1 的硬件 ring3 上，用两套页表区分彼此，这样 L2 的一切特权操作都会落回 L1。第二，L1 里放一个 switcher，负责在客户机和 hypervisor 之间快速切换。第三，L1 用影子页表把 L2 的两级翻译合成一级，交给硬件和 L0 原有的 EPT01 一起用。

在 KVM 看来，PVM 只是 Intel、AMD 之外的第三个 vendor 模块 `kvm-pvm.ko`。影子 MMU、APIC 模拟、x86 指令模拟器这些软件部件 KVM 早就有了，PVM 直接复用。上层的 `/dev/kvm` 接口不变，Cloud Hypervisor、Firecracker、Kata Containers 都能直接跑在上面。

```demo
demos/pvm-arch.html
```

## 客户机内核被放到 ring3

x86-64 上做半虚拟化，老路子是 32 位 Xen 那样把客户机内核放在 ring1。64 位下段保护基本失效，论文还提到 AMD 的新处理器和 Intel 的 x86-S 提案都在去掉 ring1、ring2，PVM 只能让客户机内核和用户态一起待在 ring3。两者靠页表隔开：PVM 规范里客户机有两个页表寄存器，`CR3` 放内核页表，一个虚拟 MSR `MSR_PVM_SWITCH_CR3` 放用户页表，切换用户态和内核态时自动对调，跟 KPTI 的两套页表是一个意思。所以 Cube 的 PVM 客户机内核配置里开着 `PAGE_TABLE_ISOLATION`，补丁里也有一条强制客户机启用 KPTI。

既然都在 ring3，硬件页表项里的 U/S 位就区分不了客户机的用户页和内核页，影子页表里统统按用户页映射，只有 hypervisor 自己的页清掉 U 位。SMEP、SMAP 这类靠 U/S 位工作的保护于是失效，规范建议客户机用 NX 位和 PKS 保护密钥补回来。

地址空间也得分。宿主内核占着高半区，客户机内核不能再链接到老地方。PVM 在高半区单独划出一段 PGD 留给客户机，连同整个低半区，通过虚拟 MSR `MSR_PVM_LINEAR_ADDRESS_RANGE` 告诉它可用范围。宿主内核占着最高的 2 GB，客户机内核只好编成位置无关的 PIE，启动时把自己重定位进这段地址。好处是宿主机和客户机的地址互不重叠，宿主内核照样能用全局页节省 TLB；switcher 的代码和数据只需映射到一个固定的地址，影子页表的根也只要从宿主页表里克隆这几项。Cube 的客户机内核配置里因此有 `X86_PIE` 和 `RELOCATABLE_UNCOMPRESSED_KERNEL`。

在 ring3 里，很多 x86 指令要么陷入，要么悄悄给出宿主机的值。PVM 规范为此改写了一部分 ABI：

- **事件投递**：中断和异常不走客户机自己的 IDT，而是像 Intel 新的 FRED 一样跳到一个固定入口。从用户态进来时，用户态的 RIP、RFLAGS、GSBASE，以及向量号、错误码、CR2，都写进一块客户机和 hypervisor 共享的结构 PVCS（PVM vCPU struct）。
- **返回用户态**：`sysret`、`iret` 在 ring3 用不了，规范定义了一条合成指令 ERETU，其实就是放在约定地址上的一条 `SYSCALL`，hypervisor 从 PVCS 里取现场恢复用户态。
- **超级调用**：客户机内核态执行 `SYSCALL` 就是超级调用，RAX 是调用号。
- **CPUID**：ring3 的 `CPUID` 默认不会陷入，会直接读到宿主机的结果。规范的办法是在它前面加一条 `invlpg` 去访问一个特殊地址。`invlpg` 是特权指令，在 ring3 必然陷入，hypervisor 借机把两条一起模拟掉。客户机内核启动时也靠这一招识别 PVM：先看自己是不是在 CPL 3、中断是不是开着，再用这条合成 CPUID 读签名。

## switcher：借用宿主内核的入口

客户机在 ring3 执行 `syscall`、触发缺页或者被中断打断，CPU 都会跳进 h_ring0 里宿主内核注册的入口，因为 MSR_LSTAR 和 IDTR 都是宿主机的。这和普通用户进程陷入内核完全一样。PVM 正是利用这一点：switcher 不是另起一套入口，而是在宿主内核现有的入口代码里加了一道判断。补丁作者把这叫作“integral entry”。

判断的依据放在每 CPU 的 TSS 后面多出来的一小块区域里，主要三项：宿主机的 CR3、宿主机的栈指针、下次进客户机要装的 CR3。其中宿主栈指针兼作标志，不为 0 就说明这颗 CPU 正跑着客户机。入口代码看到“来自 ring3 且标志不为 0”，就知道这是一次 VM exit：切回宿主页表，做 RSB 填充、IBRS 这类和硬件 VM exit 相同的推测执行防护，然后恢复宿主栈，从当初进入客户机的那个函数调用里返回。标志为 0 时，宿主机自己的进程走原来的路径，没有额外开销。

所以在 kvm-pvm 看来，VM entry 是一次函数调用，VM exit 是这次调用的返回。进入客户机时，switcher 保存几个被调用者保存的寄存器，记下宿主栈，装上客户机页表，弹出客户机寄存器，最后用 `iret` 或 `sysret` 落到 ring3。退出时客户机寄存器保存之后立即清零，再进 C 代码，免得客户机的值被推测执行利用。对宿主内核而言，客户机几乎就是一个普通用户进程。

这个设计有个直接后果：RFC 里的 switcher 和宿主机的 KPTI 暂时不能共存。Cube 的宿主机 GRUB 参数里写着 `pti=off`，原因就在这里。

### 直通

光有这些，客户机里的每次系统调用都要进出 hypervisor 两次：一次把用户态的 `syscall` 转成内核入口，一次把 ERETU 转回用户态。论文测得这样的 `getpid` 要 1.93 微秒，是硬件嵌套 KVM 的 8 倍多。

于是 switcher 多做了一步，叫 direct switch。它在每 CPU 区域里多存几项：客户机内核和用户态各自的硬件 CR3、PVCS 的地址、内核入口地址、内核栈和 GSBASE，再加一组标志位说明此刻能不能直通。条件满足时，switcher 自己把用户现场写进 PVCS、换页表、换 GSBASE，`sysretq` 直接落到客户机内核入口；ERETU 也一样，只要返回地址是约定的那个、段寄存器是常见值，就直接回用户态。kvm-pvm 全程不参与。

```demo
demos/pvm-sys.html
```

这段汇编里有个容易出事的细节：Intel CPU 上 `sysret` 遇到非规范地址的 RCX，会在 ring0 触发 #GP，而此时栈指针是客户机给的，等于把宿主机交给了客户机。switcher 每次 `sysretq` 前都把 RCX 规范化，对不上就退回 `iret`。

## CPU 和中断虚拟化

CPU 虚拟化全靠软件。客户机内核执行特权指令会触发 #GP，退到 kvm-pvm，再由 KVM 的 x86 指令模拟器解释执行。这条路很慢，所以 PVM 用 Linux 现成的 paravirt 接口 `pv_cpu_ops`、`pv_mmu_ops`、`pv_irq_ops`，把最常用的 22 种特权操作改成超级调用，比如读写 MSR、切换页表、刷 TLB、加载 GS。规范里列出的专用超级调用有 `PVM_HC_LOAD_PGTBL`、`PVM_HC_TLB_INVLPG`、`PVM_HC_RDMSR` 等。

中断是唯一绕不开 L0 的地方：物理中断来了，硬件必然先退出到 L0，L0 再注入给 L1。这和普通虚拟机一样，只有一次。之后就全在 L1 里：中断落进 switcher，转成 VM exit，KVM 的 APIC 模拟把它变成虚拟中断，再按 PVM 的事件投递送进客户机。

还剩一个问题：客户机在 ring3 执行 `cli`、`sti` 改不了真正的中断标志，硬件上的 IF 一直是 1。PVM 用 PVCS 里的一个标志位代替它：客户机关中断只是清掉这一位，不陷入；hypervisor 想注入中断时先看这一位，关着就置上“有中断待处理”。客户机重新开中断时看到这个标记，再发一次 `PVM_HC_IRQ_WIN` 超级调用把中断收走。`HLT` 也改成超级调用，睡眠和唤醒都在 L1 里完成，不用经过 L0。论文里 fluidanimate 这类阻塞同步多的并行程序，PVM 甚至比硬件嵌套还快，原因就在这里。

## 影子页表：把翻译工作留在 L1

内存虚拟化是 PVM 名字的来源。它把 L2 的两级翻译交给 L1 用影子页表合成：L2 照常维护自己的页表 GPT2，PVM 把 GPT2 和“沙箱物理地址到 L1 物理地址”的映射合成影子页表 SPT12，直接把 L2 虚拟地址翻成 L1 物理地址。硬件跑 L2 时，CR3 指向 SPT12，EPTP 还是 L0 给 L1 准备的那张 EPT01。L0 看到的就是一台普通虚拟机在换 CR3，什么都不用改。论文把这叫 PVM-on-EPT。

```demo
demos/pvm-mmu.html
```

影子页表的老问题是同步：GPT2 一变，SPT12 就得跟着变。PVM 沿用 KVM 的写保护做法，GPT2 对客户机只读，客户机每写一项都陷入 PVM。这听上去和 EPT-on-EPT 一样糟，数一下其实不然：L2 碰到一块新内存，PVM 下是 2n+4 次切换（n 是页表级数），EPT-on-EPT 是 2n+6 次；更要紧的是 PVM 的每一次都只是 L1 里的特权级变化，论文测得平均 0.179 微秒，而嵌套下一次切换要 1.3 微秒，还要排队等同一个 L0。

```demo
demos/pvm-pf.html
```

为了适配 PVM，KVM 的影子 MMU 改了几处：影子页表的级数和宿主机保持一致；分配根页表时从宿主页表里克隆 switcher 所在的那几项，这几项不按普通影子项管理，写保护和缺页处理都碰不到它们，客户机也就改写不了；处理缺页前先检查地址是否落在客户机允许的范围内；所有客户机映射都强制带 U 位，因为客户机永远在 ring3。

在此之上，论文讲了三项优化：

- **预填（prefault）**：客户机内核填完 GPT2、用 ERETU 返回用户态时，PVM 不直通，而是先进一次 hypervisor，把刚填好的映射顺手写进 SPT12。不然用户态重新访问时，还会因为影子页表缺这一项再陷入一次。
- **PCID 映射**：传统影子页表下，同一台虚拟机的所有进程共用一个 VPID，客户机刷一次 TLB 就会把整台虚拟机的 TLB 刷掉。宿主机自己用不完 PCID，PVM 就拿出 32 到 63 这一段分给客户机：论文里 32–47 给客户机内核，48–63 给客户机用户态，按根页表轮流分配。客户机每个进程的影子页表都有自己的 TLB 标签，切换时不必刷 TLB。
- **细粒度锁**：KVM 的影子 MMU 原本靠一把全局的 `mmu_lock`。PVM 把数据拆成三类，各用一把锁：影子页之间的父子关系用 meta-lock，单个影子页里的表项用 pt_lock，客户机页框到影子表项的反向映射按页框加 rmap_lock。不需要锁的工作，比如遍历影子页表，挪到锁外做。多个 vCPU 同时缺页时，不再排在一把锁后面。

论文的缺页压测里，单独加上细粒度锁就让 PVM 的扩展性超过了硬件嵌套，再加上预填和 PCID 映射更快。RFC 补丁里有 PCID 映射，并行缺页和半虚拟化 MMU 优化列在“以后再发”。

## 效果

论文的实验在阿里云的 Intel Xeon 8269CY 实例上做，客户机里跑 Kata Containers，KPTI 都开着。几组有代表性的数字：

| 测试 | 单层 KVM | 嵌套 KVM | PVM（嵌套） |
| --- | --- | --- | --- |
| 一次空超级调用往返 | 0.46 μs | 7.43 μs | 0.48 μs |
| 一次异常往返 | 1.66 μs | 9.20 μs | 2.21 μs |
| `CPUID` 往返 | 0.54 μs | 7.10 μs | 0.51 μs |
| `getpid`（直通） | 0.22 μs | 0.23 μs | 0.30 μs |
| LMbench fork，单进程 | 82 μs | 113 μs | 466 μs |
| LMbench 缺页 | 0.15 μs | 0.19 μs | 1.01 μs |

前三行是 VM exit 的代价，PVM 在嵌套环境里和单层 KVM 差不多，比硬件嵌套平均快 75% 以上，因为每次只进 L1、不进 L0。系统调用靠直通追到了 1.3 倍。后两行是 PVM 的软肋：fork 会建大量新页表却很少真正访问，LMbench 的缺页测试也不需要更新 EPT。这两种情况下硬件路径根本不用 hypervisor 插手，影子页表却每次都要陷入同步。

真实负载上，论文跑了 Kbuild、Blogbench、SPECjbb2005 和 fluidanimate，每个实例一个安全容器，并发从 1 加到 16。硬件嵌套在高并发下全部崩掉，L0 成了瓶颈；PVM 大多数情况下接近单层虚拟化。论文发表时，阿里云每天用 PVM 跑十万个以上安全容器、四十万个以上 vCPU。

## 代价和争议

PVM 最大的代价就是影子页表。频繁 fork、频繁分配释放小块内存的负载，在 PVM 下会明显变慢。论文和 RFC 都承认这一点，给出的方向是半虚拟化页表：客户机改页表时不再靠写保护陷入，而是把改过的表项地址放进一个和 hypervisor 共享的环形缓冲区，大的改动直接走超级调用。两套都用上时，写保护可以完全去掉。

RFC 在邮件列表上的反馈并不热烈。Paolo Bonzini 提了一个尖锐的问题：Xen PV 当年真正的死因是防不住各种推测执行侧信道，PVM 打算怎么办？赖江山的回答是，客户机在宿主机眼里就是 ring3 的用户进程，宿主内核对用户态的那套防护和硬件 VM exit 时的防护都能直接用上；至于客户机内部的用户态和内核之间，PVM 面向的是单租户的安全容器，要求可以放宽。Sean Christopherson 则担心合进 PVM 之后，KVM 那套复杂的影子页表代码就永远删不掉了。他认为更好的方向是在 L0 和 L1 之间定义一套半虚拟化接口。赖江山的回应是，让所有云厂商都改 L0，比开发这门技术本身还难，PVM 要的就是不依赖 L0。

现实情况是，PVM 至今没有进入主线。virt-pvm 项目维护着基于 6.12.33 的分支，社区有人移植到了更新的内核，腾讯把自己的版本放在 OpenCloudOS 内核里。用 PVM 就得同时换宿主机内核和客户机内核，而且 `kvm-pvm` 和 `kvm-intel`、`kvm-amd` 不能同时加载。

## 在 CubeSandbox 里

Cube 用 PVM 时要换两个内核，VMM 和上层组件几乎不用改：

- **宿主机内核**：基于 OpenCloudOS 内核，配置里 `CONFIG_KVM_PVM=m`。安装后加载 `kvm_pvm` 模块，`/dev/kvm` 就出现了。官方基准用的版本是 `6.6.69-opencloudos9.cubesandbox.pvm.host`。
- **宿主机启动参数**：除了上面说的 `pti=off`，还有 `no5lvl`（对应 RFC 里“五级页表还没完全实现”）、`kvm.nx_huge_pages=never`、`transparent_hugepage=never`，以及一长串屏蔽 CPU 特性的 `clearcpuid=`。
- **客户机内核**：发布包里带两份，普通的 `vmlinux` 和 `vmlinux-pvm`，安装时设 `CUBE_PVM_ENABLE=1` 才会用后者。PVM 版打开了 `PVM_GUEST`、`X86_PIE` 和客户机 KPTI，关掉了五级页表。
- **VMM**：cube-hypervisor 创建 KVM 实例后，查 KVM 支持的 MSR 列表里有没有 `MSR_PVM_VCPU_STRUCT`（`0x4b564df1`），有就把后端类型设成 `KvmPvm`。代码里对这个类型只有一处特殊处理：客户机物理地址位宽最多 43 位。seccomp 规则和硬件 KVM 共用，其余流程也一样，官方的 PVM 基准里快照、回滚、克隆都测过。

性能上，官方的 PVM 基准在腾讯云 SA9.4XLARGE32 上测（16 核 AMD EPYC 9K65，32 GiB 内存）：单并发创建平均 66.7 毫秒，20 并发平均 365 毫秒；每台沙箱额外占用约 30 MB 内存；空载沙箱打一次快照约 41 毫秒。裸金属版测试用的是 96 核的 BMI5，单并发平均 47.8 毫秒。两台机器差得太多，不能直接算出 PVM 的开销，但足以说明：在一台普通云主机上，沙箱的创建延迟仍在百毫秒以内。要留意的是沙箱里跑什么。Agent 经常起子进程，装包、跑脚本、调 shell 命令，这正是 fork 和 exec 密集的负载，按论文的数据，这类操作在 PVM 上会比硬件虚拟化慢好几倍。官方部署文档也建议，要更强的隔离和性能就选裸金属节点。ARM64 上没有 PVM，那边只能用硬件虚拟化。

## 参考

- Hang Huang, Jiangshan Lai 等，[PVM: Efficient Shadow Paging for Deploying Secure Containers in Cloud-native Environments](https://dl.acm.org/doi/10.1145/3600006.3613158)，SOSP 2023。[PDF](https://ranger.uta.edu/~jrao/papers/sosp23.pdf)。架构、switcher、PVM-on-EPT、优化与评测数据。
- Lai Jiangshan，[[RFC PATCH 00/73] KVM: x86/PVM: Introduce a new hypervisor](https://lwn.net/Articles/963718/)，2024-02-26。设计决策、补丁结构与已知限制；[完整讨论串](https://yhbt.net/lore/kvm/20240226143630.33643-2-jiangshanlai@gmail.com/T/)，包括 Paolo Bonzini 和 Sean Christopherson 的意见。
- [RFC PATCH 03/73：switcher](https://lists.openwall.net/linux-kernel/2024/02/26/1297) 与 [04/73：direct switching](https://lists.openwall.net/linux-kernel/2024/02/26/1298)。
- [X86 PVM Specification](https://github.com/virt-pvm/linux/blob/pvm/Documentation/virt/kvm/x86/pvm-spec.rst)。底层寄存器状态、PVCS、合成指令、超级调用与地址范围。
- [virt-pvm/linux](https://github.com/virt-pvm/linux) 与 [PVM 搭配 Kata Containers 上手](https://github.com/virt-pvm/misc/blob/main/pvm-get-started-with-kata.md)。
- 腾讯云，[CubeSandbox 仓库](https://github.com/TencentCloud/CubeSandbox)。本文依据 `deploy/pvm` 下的内核配置与 GRUB 脚本、`hypervisor/hypervisor/src/kvm/mod.rs` 和 `hypervisor/vmm/src/vm.rs`。
- [CubeSandbox PVM 部署](https://cubesandbox.com/zh/guide/pvm-deploy) 与 [PVM 云服务器性能基准](https://cubesandbox.com/zh/blog/posts/2026-06-03-cubesandbox-perf-benchmark-pvm)，2026-06-03。
- 本站的 [CubeSandbox 虚拟化篇](/blog/cubesandbox-virtualization/)。
