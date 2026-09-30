---
name: blog-html-demo
description: 为 myblog 的文章制作可交互的 HTML/SVG 动画演示，并通过 ```demo 代码块在构建时内联进文章。当用户想在博客文章某段落插入互动演示、动画、可点击的示意图，或提到“演示”“互动”“动画”“demo”时使用。
---

# 博客交互演示

## 机制

- 文章里写一个 `demo` 代码块，内容是演示文件相对于该 md 的路径：

  ````markdown
  ```demo
  demos/<name>.html
  ```
  ````

- `gensite/markdown_html.py` 的 `_demo_html` 在构建时读取该文件，原样包进 `<figure class="demo">` 内联到正文；文件不存在时打印 `demo missing` 并在页面显示红框。
- 演示文件统一放在 `blog/demos/`，文件名用英文短横线，如 `init-hardware.html`。
- 容器样式在 `theme/site.css` 的 `.prose .demo`，一般不用改。
- 参考实现：`blog/demos/init-hardware.html`（systemd 文章，点击依次接入鼠标、硬盘、显示器、网卡，再模拟 SysV init 串行执行脚本）。新演示先读它，沿用同样的结构。

## 工作流程

1. 读目标文章，找到用户说的段落，理解这段要讲清的概念。演示要服务于正文叙述，最好能顺势引出下一段。
2. 设计交互前先用一两句话跟用户确认：画面元素、每次点击发生什么、结束状态。用户已经说得很具体时直接做。
3. 写 `blog/demos/<name>.html`，遵守下面的约定。
4. 在该段落后插入 `demo` 代码块。不要改用户的正文文字；发现占位符（如“xxx”）只在回复里提醒。
5. 验证（见下文），截图给用户看。

## 演示文件约定

文件是一个自包含的 HTML 片段（不是完整页面），直接用浏览器打开也能调试：

```html
<div class="<name>-demo">
  <style>
    .<name>-demo { --d-fg: var(--fg, #09090b); /* 其他变量同理带兜底值 */ }
    .<name>-demo svg { display: block; width: 100%; height: auto; }
    /* 所有规则都以 .<name>-demo 开头，避免污染正文 */
  </style>
  <svg viewBox="0 0 800 400">...</svg>
  <div class="bar"><button type="button"></button><span class="caption"></span></div>
  <script>
    (function () {
      var root = document.currentScript.parentElement;
      // 只在 root 内查询元素，不用全局 id，同一页可放多个演示
    })();
  </script>
</div>
```

- **作用域**：根节点 class 用 `<name>-demo`，CSS 全部挂在它下面；JS 用 IIFE + `document.currentScript.parentElement`，不用 `id`、不写全局变量。
- **风格**：跟站点一致，黑白灰为主、少量强调色。可用站点变量 `--fg --muted --faint --line --soft --radius --sans --mono`，必须带兜底值。成功用 `#16a34a`，进行中用 `#d97706`。站点没有暗色模式。
- **尺寸**：SVG 用 `viewBox`（常用 `800x400`）自适应宽度，相当于一张配图大小；不要写死像素宽度。
- **交互**：点击画面和按钮都能推进；按钮文字提示下一步做什么；最后一步提供“重来”。状态用 `data-state` 属性驱动，CSS transition 做过渡，别手写逐帧动画。
- **文字说明**：可以配一个深色 `.log` 区域，打印仿真的内核日志、命令输出，让演示贴近真实系统；内容要技术上准确。
- **无障碍**：`svg` 加 `role="img"` 和 `aria-label`；日志区加 `aria-live="polite"`；加 `prefers-reduced-motion` 时关闭动画。
- **无依赖**：原生 JS/SVG/CSS，不引外部库、不用 ES module、不加载网络资源。

## 验证

```shell
python build.py --build-only
```

构建输出不能有 `demo missing`。然后在后台起静态服务预览（需要沙箱外权限）：

```shell
nohup python -m http.server 9399 -d docs >/tmp/demo-srv.log 2>&1 &
```

用浏览器工具打开 `http://127.0.0.1:9399/blog/<文章名 URL 编码>/`，用 `Runtime.evaluate` 滚动到 `.<name>-demo` 并依次点按钮走完整个流程，检查每一步状态，最后截图。完成后 `pkill -f "http.server 9399"`。

用户日常用 `python build.py`，它会监听 `blog/` 下 `.md` 和 `.html` 的变化并自动刷新浏览器。
