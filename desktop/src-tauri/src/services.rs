use serde::Serialize;
use serde_json::Value;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const SERVICES: &[(&str, &str)] = &[
    ("postgres", "PostgreSQL · 数据与知识索引"),
    ("redis", "Redis · 后台任务队列"),
    ("api", "FastAPI · 业务接口"),
    ("worker", "Worker · Agent 工作流"),
    ("dispatcher", "Dispatcher · 任务派发"),
    ("frontend", "前端 · 现有工作台"),
    ("workspace-sandbox", "Sandbox · 代码执行沙箱"),
];
const COMPOSE: &[&str] = &[
    "compose",
    "--project-name",
    "careerforge",
    "-f",
    "compose.yaml",
    "-f",
    "compose.offline.yaml",
    "-f",
    "compose.ide.yaml",
    "--profile",
    "app",
];

#[derive(Serialize)]
pub struct ServiceStatus {
    pub id: String,
    pub name: String,
    pub state: String,
    pub health: String,
}
#[derive(Serialize)]
pub struct DesktopStatus {
    pub project_dir: Option<String>,
    pub docker: String,
    pub message: String,
    pub services: Vec<ServiceStatus>,
    pub api_ready: bool,
    pub ui_ready: bool,
    pub sandbox_isolated: bool,
    pub ready: bool,
}

fn root_valid(path: &Path) -> bool {
    [
        "compose.yaml",
        "compose.offline.yaml",
        "compose.ide.yaml",
        "backend/pyproject.toml",
        "frontend/package.json",
    ]
    .iter()
    .all(|p| path.join(p).is_file())
        && std::fs::read_to_string(path.join("compose.yaml"))
            .map(|s| s.lines().any(|l| l.trim() == "name: careerforge"))
            .unwrap_or(false)
}
fn project_root() -> Option<PathBuf> {
    if let Some(value) = std::env::var_os("CAREERFORGE_PROJECT_DIR") {
        let path = PathBuf::from(value);
        return if root_valid(&path) {
            path.canonicalize().ok()
        } else {
            None
        };
    }
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .skip(1)
        .take(7)
        .find(|p| root_valid(p))
        .and_then(|p| p.canonicalize().ok())
}
fn docker_exe() -> PathBuf {
    #[cfg(windows)]
    if let Some(base) = std::env::var_os("ProgramFiles") {
        let path = PathBuf::from(base).join("Docker/Docker/resources/bin/docker.exe");
        if path.is_file() {
            return path;
        }
    }
    PathBuf::from("docker")
}
fn command(exe: &Path, args: &[&str], cwd: Option<&Path>, seconds: u64) -> Result<String, String> {
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    if let Some(path) = cwd {
        cmd.current_dir(path);
    }
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW: no PowerShell/CMD flashing.
    let mut child = cmd.spawn().map_err(|_| "命令不可用".to_owned())?;
    let stdout = child.stdout.take().unwrap();
    // Drain concurrently so a full pipe never blocks a child; retain only a bounded response.
    let reader = thread::spawn(move || {
        let mut source = stdout;
        let mut bytes = Vec::new();
        let mut buffer = [0; 4096];
        while let Ok(count) = source.read(&mut buffer) {
            if count == 0 {
                break;
            }
            if bytes.len() < 262144 {
                bytes.extend_from_slice(&buffer[..count.min(262144 - bytes.len())]);
            }
        }
        String::from_utf8_lossy(&bytes).into_owned()
    });
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(exit)) => {
                let text = reader.join().unwrap_or_default();
                return if exit.success() {
                    Ok(text)
                } else {
                    Err("命令未成功完成".into())
                };
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("命令状态不可用".into());
            }
            _ => {}
        }
        if started.elapsed() > Duration::from_secs(seconds) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("命令超时".into());
        }
        thread::sleep(Duration::from_millis(100));
    }
}
fn docker(args: &[&str], cwd: Option<&Path>, seconds: u64) -> Result<String, String> {
    command(&docker_exe(), args, cwd, seconds)
}
fn local_context() -> bool {
    if let Ok(host) = std::env::var("DOCKER_HOST") {
        if !host.starts_with("npipe://") && !host.starts_with("unix://") {
            return false;
        }
    }
    docker(
        &[
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ],
        None,
        5,
    )
    .map(|s| s.trim().starts_with("npipe://") || s.trim().starts_with("unix://"))
    .unwrap_or(false)
}
fn engine_ready() -> bool {
    docker(&["version", "--format", "{{.Server.Os}}"], None, 5)
        .map(|s| s.trim() == "linux")
        .unwrap_or(false)
}
fn http_body(path: &str) -> Option<String> {
    let address: SocketAddr = "127.0.0.1:8080".parse().ok()?;
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .ok()?;
    write!(
        stream,
        "GET {} HTTP/1.0\r\nHost: 127.0.0.1:8080\r\nConnection: close\r\n\r\n",
        path
    )
    .ok()?;
    let mut bytes = Vec::new();
    stream.take(65536).read_to_end(&mut bytes).ok()?;
    let response = String::from_utf8_lossy(&bytes);
    if !response.starts_with("HTTP/1.1 200 ") && !response.starts_with("HTTP/1.0 200 ") {
        return None;
    }
    response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body.to_owned())
}
fn api_ready() -> bool {
    http_body("/health/ready")
        .and_then(|body| serde_json::from_str::<Value>(&body).ok())
        .map(|value| value["status"] == "ready")
        .unwrap_or(false)
}
pub fn web_ready() -> bool {
    api_ready()
        && http_body("/")
            .map(|s| s.contains("CareerForge"))
            .unwrap_or(false)
}
fn compose(root: &Path, suffix: &[&str], seconds: u64) -> Result<String, String> {
    let mut args = COMPOSE.to_vec();
    args.extend_from_slice(suffix);
    docker(&args, Some(root), seconds)
}
fn rows(text: &str) -> Vec<Value> {
    if let Ok(Value::Array(values)) = serde_json::from_str(text) {
        return values;
    }
    text.lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}
pub fn status() -> DesktopStatus {
    let root = project_root();
    let mut value = DesktopStatus {
        project_dir: root.as_ref().map(|p| {
            p.display()
                .to_string()
                .trim_start_matches(r"\\?\")
                .to_owned()
        }),
        docker: "unavailable".into(),
        message: String::new(),
        services: SERVICES
            .iter()
            .map(|(id, name)| ServiceStatus {
                id: (*id).into(),
                name: (*name).into(),
                state: "unknown".into(),
                health: String::new(),
            })
            .collect(),
        api_ready: false,
        ui_ready: false,
        sandbox_isolated: false,
        ready: false,
    };
    let Some(root) = root else {
        value.message =
            "找不到 CareerForge 项目目录。请将程序保留在项目内，或设置 CAREERFORGE_PROJECT_DIR。"
                .into();
        return value;
    };
    if !local_context() {
        value.message =
            "Docker 不可用或当前上下文不是本机连接，请在 Docker Desktop 切回本机 Linux 引擎。"
                .into();
        return value;
    }
    if !engine_ready() {
        value.docker = "stopped".into();
        value.message = "Docker Linux 引擎未就绪，可点击启动服务。".into();
        return value;
    }
    value.docker = "running".into();
    match compose(&root, &["ps", "--all", "--format", "json"], 10) {
        Ok(text) => {
            let entries = rows(&text);
            for service in &mut value.services {
                if let Some(row) = entries
                    .iter()
                    .find(|r| r["Service"].as_str() == Some(&service.id))
                {
                    service.state = row["State"].as_str().unwrap_or("unknown").into();
                    service.health = row["Health"].as_str().unwrap_or("").into();
                } else {
                    service.state = "missing".into();
                }
            }
            if let Some(row) = entries.iter().find(|r| r["Service"] == "workspace-sandbox") {
                if let Some(name) = row["Name"].as_str() {
                    value.sandbox_isolated = docker(
                        &["inspect", "--format", "{{.HostConfig.NetworkMode}}", name],
                        None,
                        5,
                    )
                    .map(|s| s.trim() == "none")
                    .unwrap_or(false);
                }
            }
        }
        Err(_) => value.message = "无法读取 Compose 服务状态，请确认 Docker Compose 可用。".into(),
    }
    value.api_ready = api_ready();
    value.ui_ready = http_body("/")
        .map(|s| s.contains("CareerForge"))
        .unwrap_or(false);
    value.ready = value.api_ready
        && value.ui_ready
        && value.sandbox_isolated
        && value
            .services
            .iter()
            .all(|s| s.state == "running" && (s.health.is_empty() || s.health == "healthy"));
    if value.message.is_empty() {
        value.message = if value.ready {
            "所有服务已就绪，可以进入学习工作台。"
        } else {
            "部分服务尚未就绪；数据库和 Redis 显示健康检查，其余显示容器进程状态。"
        }
        .into();
    }
    value
}
fn launch_docker_desktop() -> Result<(), String> {
    #[cfg(windows)]
    {
        let base = std::env::var_os("ProgramFiles").ok_or("找不到 Docker Desktop")?;
        let executable = PathBuf::from(base).join("Docker/Docker/Docker Desktop.exe");
        if !executable.is_file() {
            return Err("请先安装并启动 Docker Desktop（Linux 容器模式）。".into());
        }
        Command::new(executable)
            .creation_flags(0x08000000)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "无法启动 Docker Desktop，请手动启动后重试。".to_owned())?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Err("请先启动 Docker 引擎。".into())
    }
}
pub fn start() -> Result<String, String> {
    let root = project_root().ok_or("找不到项目目录，请将程序保留在项目内。")?;
    if !local_context() {
        return Err("Docker CLI 不可用或上下文不是本机，未修改 Docker 配置。".into());
    }
    if !engine_ready() {
        launch_docker_desktop()?;
        let deadline = Instant::now() + Duration::from_secs(120);
        while !engine_ready() {
            if Instant::now() > deadline {
                return Err("Docker Desktop 启动超时，请检查其状态后重试。".into());
            }
            thread::sleep(Duration::from_secs(2));
        }
    }
    if web_ready() && status().ready {
        return Ok("已复用运行中的服务。".into());
    }
    compose(&root,&["up","-d","--no-build","--pull","never","postgres","redis","migrate","api","worker","dispatcher","frontend","workspace-sandbox"],180)
        .map_err(|_|"服务启动未完成。请检查本机镜像是否已构建、8080 端口是否占用，以及 Docker Desktop 状态。桌面版不会自动下载或构建镜像。".to_owned())?;
    let deadline = Instant::now() + Duration::from_secs(45);
    while !web_ready() {
        if Instant::now() > deadline {
            return Err("容器已启动，但业务接口仍未就绪；可刷新状态查看。".into());
        }
        thread::sleep(Duration::from_secs(2));
    }
    Ok("服务已启动。".into())
}

// Narrow lifecycle log: no environment variables, credentials, page content or model I/O.
pub fn record(event: &str, detail: &str) {
    if let Some(root) = project_root() {
        let directory = root.join("private/desktop");
        if std::fs::create_dir_all(&directory).is_err() {
            return;
        }
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|v| v.as_secs())
            .unwrap_or(0);
        let value = serde_json::json!({"timestamp":timestamp,"event":event,"detail":detail});
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(directory.join("startup.jsonl"))
        {
            let _ = writeln!(file, "{}", value);
        }
    }
}
