# CareerForge 桌面版（Windows x64）

保留现有 Docker 后端及无网络执行沙箱。双击项目根目录 `CareerForge.lnk` 或 `desktop/release/CareerForge.exe`。启动先显示本地服务中心，检查并启动已有服务；全部就绪后自动打开现有工作台。可通过原生窗口菜单“服务状态”返回。首次使用桌面版需正常登录，账号数据与网页版共用，登录 Cookie 独立保存。

## 使用边界

- 需要 Docker Desktop（Linux 容器）与 Microsoft WebView2 Runtime；Rust/MSVC 仅构建时需要，运行不需要。
- 仅支持本机 Docker npipe/unix 上下文，不切换上下文或连接远程 Docker。
- 固定使用项目的 compose.yaml、compose.offline.yaml、compose.ide.yaml；启动执行 up -d --no-build --pull never，不构建或下载镜像，不调用模型、运行练习命令或删除卷。
- 首次镜像仍由现有项目构建流程准备。当前开发机已有这些镜像。缺少镜像时服务中心显示可重试提示。
- 工作台打开时关闭服务中心会将其隐藏，可用菜单再次打开；关闭工作台会返回服务中心。菜单“Quit / 退出”退出桌面应用，后台服务仍继续运行；任务不受影响。此版没有自动停机、更新或系统自启动。
- 服务中心为程序自带页面，只有它能调用三个固定 Rust 命令；工作台窗口加载固定 http://127.0.0.1:8080，不授予 Tauri 文件、Shell 或服务管理权限。
- 状态展示 Docker、7 个服务容器、API readiness、页面可读性与沙箱 network_mode=none。Worker/Dispatcher 状态仅证明容器进程，不代替任务/模型健康诊断。
- 程序应保留在项目目录内；搬动时可设置 CAREERFORGE_PROJECT_DIR 为完整项目目录。程序不读取或展示 .env 密钥。启动过程的少量事件保存到 private/desktop/startup.jsonl，不含页面内容或凭据。

## 构建

本机缺少 Rust/MSVC，提供 Docker 交叉编译流程，使用 Tauri 官方文档中的 cargo-xwin / LLVM / Windows MSVC target。构建器只挂载 desktop 子目录，不挂载宿主 Docker socket、原项目材料或 .env。首次构建需要下载编译工具和 SDK。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/build.ps1
```

已有构建器时添加 `-SkipBuilder`。Cargo.lock 固定 Rust 依赖；二进制、SDK 和编译缓存不提交 Git。当前先交付项目内可执行文件和快捷方式，不制作独立安装包。

只读诊断（不启动服务、不执行代码、不调用模型）：

```powershell
& desktop/release/CareerForge.exe --diagnose "$env:TEMP/careerforge-status.json"
```

参考：[Tauri Windows 交叉编译](https://v2.tauri.app/distribute/windows-installer/#build-windows-apps-on-linux-and-macos)、[能力权限](https://v2.tauri.app/security/capabilities/)。
