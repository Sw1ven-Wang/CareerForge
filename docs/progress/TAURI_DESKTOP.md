# Tauri 本机桌面版

日期：2026-10-13。保留已有 Docker 后端、数据库、队列和执行沙箱；桌面版只新增本地服务中心与现有工作台窗口，不改变业务 workflow。

## 启动链路

双击项目内 CareerForge.lnk / desktop/release/CareerForge.exe → 加载内置服务中心 → 识别项目目录 → 检查本机 Docker 上下文与 Linux 引擎 → 必要时启动 Docker Desktop → 复用或启动固定 Compose 服务 → 校验 API readiness 和页面 → 在 Tauri WebView 中打开 http://127.0.0.1:8080。

服务中心显示 PostgreSQL、Redis、API、Worker、Dispatcher、前端、代码沙箱，以及业务接口、页面、沙箱网络隔离状态。窗口菜单可以返回服务中心；重复启动使用 single-instance 插件聚焦原窗口。关闭程序保留共享后台服务，不取消已有任务或停止数据库。

## 本机边界

服务中心只暴露 desktop_status / start_services / open_workspace 三个 Rust 命令，不接受命令文本、任意 URL 或文件路径。能力权限限定到内置 manager 窗口；加载已有 HTTP 工作台的 workspace 窗口没有本机命令权限。不增加 Shell、文件系统或 Docker socket 的网页授权。

Compose 固定项目名 careerforge，固定 compose.yaml + compose.offline.yaml + compose.ide.yaml。启动只用 up -d --no-build --pull never；不自动构建镜像、下载镜像、清理数据或执行练习。仅允许本机 npipe/unix Docker 连接；拒绝远程 DOCKER_HOST / context，且不改用户 Docker 设置。

状态读取只返回白名单字段，不读取 .env。PostgreSQL/Redis 有容器健康检查，其余运行状态只证明容器进程存在；额外检查 API readiness（含数据库查询）、前端页面可读性和沙箱 network_mode=none，不把 Worker 进程运行宣称为所有任务可成功执行。

网页账号与数据直接复用，桌面 WebView 的登录会话与浏览器独立。API 保持原有登录、Origin、CSRF、幂等与版本校验。模型和代码执行仍由原工作流控制，桌面启动不产生模型调用。

## 构建与交付

本机具备 Docker / WebView2 154.0.4258.62，缺少 Rust/MSVC。使用 Tauri 官方 Windows 交叉编译方案，在隔离构建容器内通过 Rust 1.99.0、LLVM 19、cargo-xwin 0.23.1 编译 x86_64-pc-windows-msvc。只挂载 desktop 子目录，不挂载 .env、原始项目材料或 Docker socket。Cargo.lock 固定应用依赖（Tauri 2.12.3、tauri-build 2.7.2、single-instance 2.5.3）。

构建入口 desktop/build.ps1。交付为项目内 Windows 可执行文件和快捷方式，二进制及 SDK 缓存忽略 Git。需要保留项目目录、现有镜像、Docker Desktop 和 WebView2；不是免依赖安装包，也不是离线本地大模型。详细操作见 ../../desktop/README.md。

## 本轮必要验证

仅做 JavaScript 语法检查、Rust release 编译及一次本机启动 / 服务状态 / 页面接入验证。未运行全量回归、真实模型请求或练习命令。结果：Windows release 编译通过（交叉链接仅提示 SDK 的调试 PDB 未打包，不影响可执行文件生成），JS 语法检查通过。真实本机诊断显示 7 个服务 running，PostgreSQL / Redis healthy，API ready、页面可读与沙箱无网络均为 true。

实际启动后，应用自身生命周期日志记录 manager 加载、start_services requested/completed、workspace.open completed 和 workspace 页面加载；证明内置页面的 Rust IPC、现有服务复用及真实 WebView 页面接入已走通。随后补齐关闭窗口返回服务中心的行为，重新编译并启动交付版本。没有停止 Docker 或数据库来制造冷启动场景，没有额外验证业务流程或窗口各项交互。

本机收据、状态快照和启动日志位于 ignored private/desktop/。可执行文件约 7 MB，项目内快捷方式已生成。启动日志只保存时间、固定事件名和窗口标签，不含 URL、页面正文、环境变量或凭据。
