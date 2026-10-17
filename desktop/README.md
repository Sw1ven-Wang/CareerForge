# CareerForge 桌面版（Windows x64）

保留现有 Docker 后端及无网络执行沙箱。双击项目根目录 `CareerForge.lnk` 或 `desktop/release/CareerForge-0.2.0.exe`。启动先显示本地服务中心，检查并启动已有服务；全部就绪后自动打开现有工作台。可通过原生窗口菜单“服务状态”返回。首次使用桌面版需正常登录，账号数据与网页版共用，登录 Cookie 独立保存。

## 使用边界

- 需要 Docker Desktop（Linux 容器）与 Microsoft WebView2 Runtime；Rust/MSVC 仅构建时需要，运行不需要。
- 仅支持本机 Docker npipe/unix 上下文，不切换上下文或连接远程 Docker。
- 固定使用项目的 compose.yaml、compose.offline.yaml、compose.ide.yaml；启动执行 up -d --no-build --pull never，不构建或下载镜像，不调用模型、运行练习命令或删除卷。
- 首次镜像仍由现有项目构建流程准备。新克隆的源码不附带镜像，需先按根目录 README 构建。缺少镜像时服务中心显示可重试提示。
- 工作台打开时关闭服务中心会将其隐藏，可用菜单再次打开；关闭工作台会返回服务中心。菜单“Quit / 退出”退出桌面应用，后台服务仍继续运行；任务不受影响。此版没有自动停机、更新或系统自启动。
- 服务中心为程序自带页面，只有它能调用三个固定 Rust 命令；工作台窗口加载固定 http://127.0.0.1:8080；仅学习 IDE 路由授予选定目录的专用读写命令，不授予通用文件、Shell 或服务管理权限。
- 状态展示 Docker、7 个服务容器、API readiness、页面可读性与沙箱 network_mode=none。Worker/Dispatcher 状态仅证明容器进程，不代替任务/模型健康诊断。
- 程序应保留在项目目录内；搬动时可设置 CAREERFORGE_PROJECT_DIR 为完整项目目录。程序不读取或展示 .env 密钥。启动过程的少量事件保存到 private/desktop/startup.jsonl，不含页面内容或凭据。

## 构建

提供无需本机 Rust/MSVC 的 Docker 交叉编译流程，使用 Tauri 官方文档中的 cargo-xwin / LLVM / Windows MSVC target。构建器只挂载 desktop 子目录，不挂载宿主 Docker socket、原项目材料或 .env。首次构建需要下载编译工具和 SDK。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/build.ps1
```

已有构建器时添加 `-SkipBuilder`。Cargo.lock 固定 Rust 依赖；二进制、SDK 和编译缓存不提交 Git。当前先交付项目内可执行文件和快捷方式，不制作独立安装包。

只读诊断（不启动服务、不执行代码、不调用模型）：

```powershell
& desktop/release/CareerForge-0.2.0.exe --diagnose "$env:TEMP/careerforge-status.json"
```

参考：[Tauri Windows 交叉编译](https://v2.tauri.app/distribute/windows-installer/#build-windows-apps-on-linux-and-macos)、[能力权限](https://v2.tauri.app/security/capabilities/)。

## 本机 IDE 与两类资料（2026-10-13 更新）

主导航收敛为学习工作台、需求资料、开源项目。需求由用户粘贴正文或上传文本文件，支持岗位、技术资料、文档与笔记；不再新建招聘采集/调研任务。选择需求和已导入开源项目，Agent 分析需求后自主查找代码，展示需求原文与实现位置的对应。计划、岗位统计与运行记录放入次级入口。

在学习工作台切换“自己的 IDE → 本机项目”，使用原生目录选择器授权具体目录。可打开、新建与直接保存 UTF-8 源码文本；单文件最多 100 KB，列表最多 1000 项，超限时提示缩小目录。拒绝私密/依赖目录、设备路径、越界、symlink/junction 与硬链接。磁盘内容被其他编辑器改变时拒绝覆盖，需重新读取。

Agent 只接收明确选择的最多 3 个已保存文件，发送前展示文件清单。内容以一次明确操作复制到本机后端快照，不扫描或上传整个目录。补丁先在网页审阅差异，再经原生确认写入选定目录；所有文件先检查旧正文和磁盘哈希，再备份后写入。保存保留已有 UTF-8 BOM 和 CRLF；备份清单与原始字节放在应用数据目录 code-backups 中，可据清单手动恢复。此版不通过 Agent 删除本机文件。

多文件写入不是跨文件崩溃原子事务：写入错误会尝试恢复，进程/断电中断时使用写入前备份核对。若本机写入成功但后端记录同步失败，界面明确显示待同步，允许重试记录，避免再次写文件。

运行命令仍逐条确认，只执行明确选择的文件快照，使用原有无网络 Docker 沙箱。未提供宿主 Shell、任意本机命令、依赖安装、自动执行或完整 VS Code/LSP 功能。目录授权与工作区绑定限当前桌面会话；重启或退出后需重新选择目录，不自动恢复本机读写权限。

旧桌面窗口正在使用时，构建发布为新的版本文件，不强行退出或覆盖旧程序。保存未完成编辑后从菜单退出旧版，再双击根目录快捷方式进入新版。
