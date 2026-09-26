# 个人博客

博客、笔记、工具、游记和关于我，构建成 `docs/` 里的静态网页。

- `blog/`：首页文章列表。每篇的创建时间写在 `blog/time.yaml`，构建时用来排序和显示
- `note.md`：笔记页
- `tools.md`：工具页
- `about-me.md`：本人
- `travel/`：游记。`travel/四川/成都/readme.md` 会点亮四川，`travel/重庆/readme.md` 会点亮重庆

## 本地构建与预览

```shell
python build.py
```

脚本会把页面写到 `docs/`，在 `9381` 端口启动服务器并打开浏览器。只构建、不启动服务器：

```shell
python build.py --build-only
```
