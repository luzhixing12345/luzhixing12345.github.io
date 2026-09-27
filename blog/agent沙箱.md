
# agent 沙箱

随着 agent 发展至今，我们普遍认为一个比较成熟完善的 agent 的 harness system 应当包含以下几个组件：完善的 context 上下文管理，稳定的 tools 约束定义，memory 记忆功能，observability 可观测性以及 Eval 校验模块。

![20260901103511](https://raw.githubusercontent.com/learner-lu/picbed/master/20260901103511.png)

这些模块各有各的用途，但是本文的重点会围绕「agent 沙箱」展开，~~一方面是因为我们团队就是做 agent 沙箱业务的~~，另一方面是因为沙箱是 agent 和外界交互的关键的执行层。控制层关心下一步做什么，执行层获取更多信息反馈给控制层，并指导控制层做进一步的决策。

![Clipboard_Screenshot_1790234962](https://raw.githubusercontent.com/learner-lu/picbed/master/Clipboard_Screenshot_1790234962.png)

本文并不会深入沙箱的实现细节，而是会从一个宏观的角度回答一下现有的 agent 沙箱「何时用」以及 「怎么用」两个问题。

## agent沙箱何时用？怎么用？

关于「何时用」沙箱有一个很经典的问题就是"**我们为什么要用沙箱？直接在环境里面执行命令不行么？**"

一个很经典的回答是：**agent 现在有幻觉，权限太高，可能会误删除文件或者执行危险的命令，用沙箱就可以把环境隔离开，就安全了**。

「沙箱」指的是一种隔离的执行环境，让程序能够在受控的边界内运行，避免其行为影响宿主机或其他程序。顾名思义，我们通常就认为它是安全的，那么沙箱是用来解决安全问题吗？以及它真的能解决安全问题吗？

### agent的使用场景

我们先来考虑研发中一个最常见的场景，自己的电脑里打开一个 agent 应用，比如 codex。此时 codex 可以访问本地文件，可以进行网络请求。

**我们普遍认为这是存在安全风险的**，因为如果 agent 想要作恶，那么它可以删除本地的重要文件，可能会把你的私有代码甚至密钥直接发送到网络上（[zcode前车之鉴](https://mp.weixin.qq.com/s/6aRf_4e_jMAYMqrkrWS9mw)）

![20260924153925](https://raw.githubusercontent.com/learner-lu/picbed/master/20260924153925.png)

**那么既然存在风险是不是使用沙箱就可以解决安全风险呢？**但实际上几乎没有人会主动打开一个Docker容器或者说打开一个虚拟机把自己的Claude Code之类的这样的主力的agent的程序运行在里边，我相信绝大部分的用户在当前此时此刻其实都不是这样的一种使用的形态。

那么为什么没有这么做呢，**最主要的原因就是麻烦**，虚拟机确实隔离了环境，但是用着用着就会发现还是需要各种文件/权限/配置/密钥，等于又在虚拟机里面装了一遍环境，久而久之这个虚拟机又变成了另一个工作台，结果 agent 还是有可能在里边删除掉你关键的文件或者执行危险的操作，绕了一大圈好像只是起到了一个形式主义。

事实上目前对于agent的很多安全问题，我们并不是通过沙箱在解决它，而是通过应用层面的权限在解决它。这些agent设计了一套权限机制，通过这种「**审计**」的方式，让用户在中间确认这样的一些安全风险。

![20260924160021](https://raw.githubusercontent.com/learner-lu/picbed/master/20260924160021.png)

> 相信如果没有给 full access，在 agent 执行命令的时候你应该见过类似的申请权限的操作

与此同时现在的 agent 越来越擅长写大段的脚本来处理一个复杂的逻辑，为了安全而去审计每一条命令可以说愚蠢的可笑。在95%以上的场景甚至99%以上的场景这些命令都是无害的，强行开启审计无异于自断一臂。

---

那么这些 code agent 真的使用了沙箱么？**事实上这些它们用的只是最轻量的沙箱功能**，它们会使用操作系统本身提供的隔离功能，在 linux 下使用 bubblewrap + landlock，在 mac 下使用 seatbelt，在 windows 下使用受限令牌等方式。

![Clipboard_Screenshot_1790240798](https://raw.githubusercontent.com/learner-lu/picbed/master/Clipboard_Screenshot_1790240798.png)

> 除了主动要求用户审计，还有一些 agent 会做一些简单的正则判断或者命令检查，判断是不是在执行一些高危操作（比如 rm -rf /）

以 linux 为例，如果不加任何约束直接执行代码 python test.py，那么 test.py 中可以看到和访问主机上的所有内容（如下图左侧所示），Bubblewrap（bwrap）负责“给进程造一个隔离出来的世界”，Landlock 负责“限制这个进程在文件系统里能干什么”。bwarp 可以限制这个进程只能看到 /workspace /usr /dev 这几个目录，一些敏感的目录比如 ~/.ssh ~/.config 这个进程直接就访问不到了。而 landlock 可以明确限制哪些目录可读，哪些目录可写，这样进程想去修改 /usr 也会发现没有权限操作。

![Clipboard_Screenshot_1790241263](https://raw.githubusercontent.com/learner-lu/picbed/master/Clipboard_Screenshot_1790241263.png)

> 如今的本地的 code agent，例如 cursor, codex, claude code 都是通过这种非常轻量级的进程限制的方式来做的安全隔离，严格来说这种并不属于我们今天提到的沙箱技术。

### agent in sandbox

前文我们虽然提到了在日常使用的过程中不会主动讲

- 云 agent （Github Action 自动 pr）
- 长程任务 / persistent agent （不要动我本机的环境）
- AgentRL (在 sandbox 中训练，拿到 reward，用 RL 更新底层模型)
- Computer Use / GUI agent (浏览器/桌面)


目前这种 agent in sandbox 的形态通常不是承载了大量复杂业务。不论是个人的还是生产级别的复杂业务的主流的形态，只是大家目前感觉这是一种比较安全的模式。但是这个边界到底应该怎么定义，怎么样不要限制过度，导致这个agent变得什么都干不了，还有很远的路要走。

但是并不是说 agent in sandbox 这种场景不存在。实际上

长程任务 / persistent agent
AgentRL (在 sandbox 中训练，拿到 reward，用 RL 更新底层模型) 

> DeepSWE,它从 Qwen3-32B 出发，不依赖额外 SFT，而是进行 Agentic RL，让模型自己完成软件工程任务。其公开资料称，仅约 200 steps 的 RL training 就带来了显著的 SWE-bench Verified 提升；最终报告的 Pass@1 为 42.2%，结合 test-time scaling 达到 59%
>
> [DeepSWE-preview huggingface](https://huggingface.co/agentica-org/DeepSWE-Preview/blob/ee7153d63ecbfdf093e4482b4d874d77666e33a9/README.md?utm_source=chatgpt.com)
Computer Use / GUI agent (浏览器/桌面)


### sandbox as a tool

把沙箱当做 agent 的一个调用的工具是其最常见的使用场景，我们倾向于将主机上的一些资源临时放到一个沙箱中，在沙箱当中安全的执行命令，然后再把最终的结果返回回来，此时这个沙箱可以选择立即销毁，按照 session 销毁或者按照 task 销毁，和 agent 的实际生命周期解耦。

![20260928020427](https://raw.githubusercontent.com/learner-lu/picbed/master/20260928020427.png)

很多用户会访问该 BI 服务，查询数据，然后生成 html 页面预览查看

![20260928020438](https://raw.githubusercontent.com/learner-lu/picbed/master/20260928020438.png)

隔离用户数据

![20260928020445](https://raw.githubusercontent.com/learner-lu/picbed/master/20260928020445.png)

## codex as a platform

环境管理。不同任务可能需要浏览器、Shell、代码仓库、数据库甚至 GUI 环境。AgentRL 用统一的 function-call API、容器化环境、中央 Controller 和 Task Worker 来管理这些异构环境；Controller 可以单独部署，而多个 Task Worker 可以分布在不同机器甚至不同集群

多轮 rollout。模型不是生成一个答案，而是在环境中不断 interaction，采集完整 trajectory

## 总结

agentfs agentenv

GPT-5-Codex
OpenAI明确指出GPT-5-Codex的训练流程专注于真实编程环境中的复杂任务，例如“多文件重构”“运行测试套件”与“提交PR”

Tongyi Deepreasearch
在阿里通义的技术报告中，Tongyi DeepResearch 明确强调其模型“专为智能体任务训练”而设计

Cursor 2.0 Composer
设计目标是将自然语言直接转化为可运行、可维护的完整项目。该工具以持续的、交互式的代码生成与编辑为闭环，融合了意图理解、代码生成与动态调试等多个环节



fork clone rollback 用不上？

## 参考

- [openai harness-engineering](https://openai.com/zh-Hans-CN/index/harness-engineering/)
- [anthropic harness-design-long-running-apps](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [deepseek-harness 项目深度解读](https://zhuanlan.zhihu.com/p/2071362726442673749)
- [csdn deepseek](https://deepseek.csdn.net/6a7f254310ee7a33f29b0548.html)
- [Harness 是什么？为什么你需要 Harness，而不是更多插件生态](https://zhuanlan.zhihu.com/p/2007096193025602652)
- [【纯干货】万字长文教你如何打造自己的 Harness Engineering](https://km.woa.com/articles/show/656822)
- [Agent Harness 是什么？怎么做？深度实战解读](https://app.koala-oss.club/videos/0ebdeae2-03f8-433f-88b3-f7323add2b8e)
- [为什么要 Harness Engineering：它不是补丁，是分工](https://zhuanlan.zhihu.com/p/2026619084851146805)
- [全文首发deepseek harness背后的秘密，Cordis的设计哲学深度解读](https://zhuanlan.zhihu.com/p/2071345388896924946)
- [Agentic RL全流程技术分析与总结（两万字）](https://zhuanlan.zhihu.com/p/1985054130469888615)