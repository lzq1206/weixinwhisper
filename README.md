# weixinwhisper：微信朋友圈逐年导出工具

这是一个面向 **Windows 10/11 64 位** 的微信朋友圈本地缓存导出工具，程序名称为 **微信朋友圈导出工具1.0**。它从电脑版微信已经写入本机的缓存中读取朋友圈，按年份分别生成 HTML；每年页面中的照片都可以点击打开原图。

程序包含图形界面、自动定位微信数据、自动获取数据库密钥、F9 辅助刷新和可中断导出功能。仓库同时提供已经打包好的单文件 EXE。

> 朋友圈内容只取决于微信本机已经缓存的内容。程序不能恢复从未加载到本机的历史，也不会绕过微信登录或访问他人账号。

## 功能

- 按年份独立导出 HTML，不把全部内容导出后再拆分。
- 每个年度目录包含自己的 HTML、`figure/` 照片资源和导出日志。
- 自动生成 `年度索引.html`，点击年份进入对应年度。
- HTML 中的照片支持点击打开原图。
- 自动定位微信数据目录，不在界面中填写微信路径。
- 自动使用本机已保存的数据库密钥；没有保存密钥时尝试自动获取，不在界面中填写密钥。
- 辅助刷新支持 F9 开始、再次按 F9 结束，默认每 1 秒向微信朋友圈发送一次 PgUp。
- 导出过程中可以点击“停止导出”，中断当前抓取并保留已完成的年度结果。
- 可选择只导出自己的朋友圈、保留点赞评论，以及是否允许联网补全缺失图片。
- Python、Tk 图形界面和项目依赖均可打包进单文件 EXE。
- 自动定位优先检查标准微信目录，避免启动导出时扫描整块磁盘。
- 界面实时显示当前阶段、总体进度、当前年度条数，以及最新导出的朋友圈和日期。

## 直接使用 EXE

下载仓库中的 [`dist/wxMoments.exe`](dist/wxMoments.exe)，不需要另外安装 Python。

### 导出步骤

1. 启动电脑版微信并登录，进入自己的朋友圈。
2. 双击 `dist/wxMoments.exe`。
3. 选择输出目录和年份范围。微信数据目录、数据库密钥由程序自动处理。
4. 如果需要先加载历史缓存，在“朋友圈辅助刷新”区域启用 F9 辅助刷新。
5. **先切换到微信朋友圈界面，并点击左上角切换到最早的一条。**
6. 按 `F9` 开始辅助刷新；程序会按照间隔发送 PgUp。
7. 再次按 `F9` 结束辅助刷新。微信窗口必须保持在前台；切换到其他窗口后辅助刷新会暂停。
8. 等微信缓存加载完成后，点击“开始逐年导出”。
9. 大量内容导出时可以点击“停止导出”中断抓取。

辅助刷新只负责重复按键，不判断朋友圈日期。是否已经到达最新帖子，应由用户在微信界面确认。

### 默认选项

- 默认年份起点：2012。
- 默认年份终点：当前年份。
- 默认只导出自己的朋友圈。
- 默认保留点赞和评论。
- 默认不联网补图，只使用本地缓存。
- 默认 PgUp 间隔：1 秒。

### 输出结构

每次导出会在输出目录建立一个时间目录，例如：

```text
wxMoments_20260915_120000/
├─ 年度索引.html
├─ 年度统计.json
├─ 2012/
│  ├─ 朋友圈_2012.html
│  ├─ figure/
│  └─ export.log
├─ 2013/
│  ├─ 朋友圈_2013.html
│  ├─ figure/
│  └─ export.log
└─ ...
```

打开 `年度索引.html` 即可按年份浏览。点击 HTML 中的照片会在新窗口打开对应本地图片。

## 自动定位和隐私

程序会优先检查常见的 `xwechat_files`、`WeChat Files`、`Weixin Files` 和微信应用数据目录，再对当前用户目录下名称明显属于微信的目录做有限范围兜底检测。程序不会再把所有磁盘根目录作为起点递归扫描，因此“正在自动定位微信账号”通常会很快结束。数据库密钥不显示在设置中，成功获取后只保存在本机运行目录中。

如果定位阶段仍需停止，可以直接点击“停止导出”；定位流程会检查停止请求并退出，不需要强制结束程序。找不到账号时，请先启动并登录电脑版微信，确认已经加载过朋友圈缓存，再重新点击导出。

数据库密钥扫描在 Windows 单文件程序中使用安全的单进程 DLL 扫描方式，避免扫描辅助进程重复打开 GUI 窗口。自动 key 获取失败时，程序只显示一次可读的错误信息，不会连续启动多个程序窗口。

程序配置和运行缓存位于：

```text
%APPDATA%\wxMoments\
```

不要把真实微信数据库、密钥、运行缓存、朋友圈 HTML、照片或导出目录提交到 GitHub。

## 从源码打包

需要 Windows 10/11 64 位和 Python 3.10+，建议使用 Python 3.12。

```powershell
py -3.12 -m venv runtime\.venv312
runtime\.venv312\Scripts\python.exe -m pip install -r config\requirements.txt
runtime\.venv312\Scripts\python.exe -m pip install pyinstaller
.\build_windows.ps1
```

打包结果：

```text
dist\wxMoments.exe
```

打包配置位于 [`wxmoments_gui.spec`](wxmoments_gui.spec)，GUI 入口位于 [`wxmoments_gui.py`](wxmoments_gui.py)。`build_windows.ps1` 会使用仓库内的虚拟环境重新生成 EXE。

## 开发检查

```powershell
$env:PYTHONPATH = ".;src"
runtime\.venv312\Scripts\python.exe -m py_compile wxmoments_gui.py src\wxmoments.py
runtime\.venv312\Scripts\python.exe -c "import wxmoments, wxmoments_gui; print('IMPORT_OK')"
```

导出核心仍然可以被命令行脚本调用；`src/wxmoments.py` 中的 `stop_event` 支持桌面界面中断正在进行的图片抓取。

## 参考项目和来源说明

本项目在开发时参考了以下公开项目：

1. [claudemt/wxMoments](https://github.com/claudemt/wxMoments)

   参考朋友圈数据库读取、媒体处理和 HTML/PDF 导出基础实现。原项目使用 MIT License。

2. [Hankxinyu/wxMoments](https://github.com/Hankxinyu/wxMoments)

   参考当前使用的微信数据库解密、图片密钥获取、SNS 解析和朋友圈导出实现。本仓库保留原项目的 MIT License，并在此基础上增加图形界面、逐年导出、照片点击链接、停止导出和 Windows 打包配置。

3. [taojy123/KeymouseGo](https://github.com/taojy123/KeymouseGo)

   参考自动操作工具的重复键盘动作、启动/停止和快捷键交互思路。本项目没有复制其源代码；F9 辅助刷新是针对微信朋友圈场景重新实现的功能。

详细许可信息见 [`LICENSE`](LICENSE)。第三方依赖仍以各自许可证为准。

## 限制

- 正式支持 Windows 10/11 64 位和 Windows 版微信。
- 手机微信、网页版微信、macOS 和 Linux 不在支持范围内。
- 朋友圈缓存不完整时，导出结果也会不完整。
- 视频默认导出已缓存的视频封面，不保存可播放视频本体。
- 点赞和评论只能导出微信已经缓存的数据。
- 图片清晰度受本机缓存版本限制。
- F9 辅助刷新要求用户先把微信朋友圈切换到最早的一条，并保持微信窗口在前台。

## 后续同步方式

后续修改应同时更新源码、`README.md` 和 `dist/wxMoments.exe`（如果修改影响 EXE），然后提交并推送到本仓库。不要提交个人配置、运行缓存或真实朋友圈数据。
