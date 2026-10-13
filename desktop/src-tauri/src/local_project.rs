//! Selected-root text editing. No shell, process execution or arbitrary-root IPC.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::Mutex,
};
#[cfg(windows)]
use std::{
    os::windows::fs::{MetadataExt, OpenOptionsExt},
    os::windows::io::AsRawHandle,
};
use uuid::Uuid;

pub const FILE_LIMIT: u64 = 100_000;
const BLOCKED: &[&str] = &[
    ".git",
    ".env",
    ".ssh",
    ".aws",
    ".codex",
    ".agents",
    ".azure",
    ".kube",
    ".config",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "dist",
    "build",
    "vendor",
    ".careerforge-backups",
];
#[derive(Clone, Serialize)]
pub struct ProjectInfo {
    pub project_id: String,
    pub name: String,
    pub directory: String,
    pub files: Vec<String>,
    pub limited: bool,
}
#[derive(Clone)]
pub struct Project {
    pub id: String,
    root: PathBuf,
    backups: PathBuf,
    receipts: BTreeMap<String, Receipt>,
}
pub struct LocalProjects {
    pub selected: Mutex<Option<Project>>,
    pub backups: PathBuf,
}
impl LocalProjects {
    pub fn new(backups: PathBuf) -> Self {
        Self {
            selected: Mutex::new(None),
            backups,
        }
    }
}
#[derive(Clone, Serialize)]
pub struct TextFile {
    pub path: String,
    pub content: String,
    pub hash: String,
}
#[derive(Deserialize, Clone)]
pub struct Edit {
    pub path: String,
    pub before: Option<String>,
    pub after: Option<String>,
    pub expected_hash: Option<String>,
}
#[derive(Clone, Serialize)]
pub struct Receipt {
    pub operation_id: String,
    pub backup_directory: String,
    pub files: Vec<TextFile>,
}
#[derive(Serialize)]
pub struct Snapshot {
    pub files: BTreeMap<String, String>,
    pub hashes: BTreeMap<String, String>,
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn normalized(bytes: &[u8]) -> Result<String, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "仅支持 UTF-8 文本文件")?;
    if text.contains('\0') {
        return Err("不支持二进制文件".into());
    }
    Ok(text
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n"))
}
fn encoded(text: &str, before: Option<&[u8]>) -> Result<Vec<u8>, String> {
    if text.contains('\0') || text.len() > FILE_LIMIT as usize {
        return Err("文本超过 100 KB 或包含二进制内容".into());
    }
    let text = text
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let mut result = Vec::new();
    if before.is_some_and(|v| v.starts_with(&[0xef, 0xbb, 0xbf])) {
        result.extend_from_slice(&[0xef, 0xbb, 0xbf]);
    }
    let crlf = before.is_some_and(|v| v.windows(2).any(|w| w == b"\r\n"));
    result.extend_from_slice(
        if crlf {
            text.replace('\n', "\r\n")
        } else {
            text
        }
        .as_bytes(),
    );
    if result.len() > FILE_LIMIT as usize {
        return Err("写入后的文件超过 100 KB".into());
    }
    Ok(result)
}
fn allowed(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 240
        || value.contains(['\\', ':', '\0', '*', '?', '"', '<', '>', '|'])
        || value.starts_with('/')
    {
        return Err("只能使用项目内相对路径".into());
    }
    for part in value.split('/') {
        let lower = part.to_lowercase();
        let stem = lower.split('.').next().unwrap_or("");
        if part.is_empty()
            || matches!(part, "." | "..")
            || part.ends_with(['.', ' '])
            || part.chars().any(|c| c.is_control())
            || BLOCKED.contains(&lower.as_str())
            || lower.starts_with(".env.")
            || matches!(stem, "con" | "prn" | "aux" | "nul")
            || (stem.len() == 4
                && (stem.starts_with("com") || stem.starts_with("lpt"))
                && stem.ends_with(|c: char| c.is_ascii_digit()))
        {
            return Err("路径包含私密目录、设备名或非法片段".into());
        }
    }
    let path = Path::new(value);
    let name = path.file_name().unwrap().to_string_lossy().to_lowercase();
    let ext = path
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    if ![
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "vue",
        "rs",
        "go",
        "java",
        "c",
        "cpp",
        "h",
        "hpp",
        "cs",
        "json",
        "md",
        "rst",
        "txt",
        "toml",
        "yaml",
        "yml",
        "html",
        "css",
        "sql",
        "xml",
        "properties",
        "sh",
    ]
    .contains(&ext.as_str())
        && ![".gitignore", "dockerfile", "makefile", "license", "notice"].contains(&name.as_str())
    {
        return Err("本版仅支持源码和文本文件".into());
    }
    Ok(())
}
fn reparse(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}
fn options(write: bool, directory: bool) -> OpenOptions {
    let mut options = OpenOptions::new();
    options.read(true).write(write);
    #[cfg(windows)]
    {
        options
            .share_mode(if directory { 3 } else { 1 })
            .custom_flags(0x00200000 | if directory { 0x02000000 } else { 0 });
    }
    #[cfg(not(windows))]
    {
        let _ = directory;
    }
    options
}
// Pin every directory from the drive to the target parent. Windows share mode
// excludes delete/rename and reparse-point opens never follow junctions.
fn guards(root: &Path, relative: &Path, create: bool) -> Result<Vec<File>, String> {
    let mut paths: Vec<PathBuf> = root.ancestors().map(Path::to_path_buf).collect();
    paths.reverse();
    let mut next = root.to_path_buf();
    for part in relative.components() {
        next.push(part);
        paths.push(next.clone());
    }
    let mut result = Vec::new();
    for path in paths {
        if !path.exists() && create {
            fs::create_dir(&path).map_err(|_| "无法创建项目子目录")?;
        }
        let metadata = fs::symlink_metadata(&path).map_err(|_| "项目目录不可用")?;
        if !metadata.is_dir() || reparse(&metadata) {
            return Err("拒绝符号链接或 junction 目录".into());
        }
        let file = options(false, true)
            .open(&path)
            .map_err(|_| "目录正在被移动、修改或不可读")?;
        if reparse(&file.metadata().map_err(|_| "目录状态不可用")?) {
            return Err("目录链接已变化".into());
        }
        result.push(file);
    }
    Ok(result)
}
fn open_text(path: &Path, write: bool) -> Result<File, String> {
    let meta = fs::symlink_metadata(path).map_err(|_| "文件不存在或不可读")?;
    if !meta.is_file() || reparse(&meta) || meta.len() > FILE_LIMIT {
        return Err("拒绝链接、非文本候选或超过 100 KB 的文件".into());
    }
    let file = options(write, false)
        .open(path)
        .map_err(|_| "文件正在被其他程序修改或没有访问权限")?;
    let meta = file.metadata().map_err(|_| "文件状态不可用")?;
    if !meta.is_file() || reparse(&meta) || meta.len() > FILE_LIMIT {
        return Err("文件状态已变化".into());
    }
    #[cfg(windows)]
    unsafe {
        let mut info: windows_sys::Win32::Storage::FileSystem::BY_HANDLE_FILE_INFORMATION =
            std::mem::zeroed();
        if windows_sys::Win32::Storage::FileSystem::GetFileInformationByHandle(
            file.as_raw_handle() as _,
            &mut info,
        ) == 0
            || info.nNumberOfLinks != 1
        {
            return Err("拒绝硬链接或无法确认身份的文件".into());
        }
    }
    Ok(file)
}
fn bytes(file: &mut File) -> Result<Vec<u8>, String> {
    file.seek(SeekFrom::Start(0)).map_err(|_| "读取失败")?;
    let mut data = Vec::new();
    file.take(FILE_LIMIT + 1)
        .read_to_end(&mut data)
        .map_err(|_| "读取失败")?;
    if data.len() > FILE_LIMIT as usize {
        return Err("文件过大".into());
    }
    Ok(data)
}
impl Project {
    pub fn new(root: PathBuf, backups: PathBuf) -> Result<Self, String> {
        let root = root.canonicalize().map_err(|_| "目录不可用")?;
        if root.parent().is_none()
            || std::env::var_os("USERPROFILE")
                .is_some_and(|v| PathBuf::from(v).canonicalize().ok().as_ref() == Some(&root))
        {
            return Err("请选择具体项目，不能选择磁盘根目录或整个用户目录".into());
        }
        if root.components().any(|c| match c {
            std::path::Component::Normal(v) => {
                let name = v.to_string_lossy().to_lowercase();
                BLOCKED.contains(&name.as_str()) || name.starts_with(".env.")
            }
            _ => false,
        }) {
            return Err("不能打开私密目录或依赖目录".into());
        }
        let _guards = guards(&root, Path::new(""), false)?;
        Ok(Self {
            id: Uuid::new_v4().to_string(),
            root,
            backups,
            receipts: BTreeMap::new(),
        })
    }
    pub fn info(&self) -> Result<ProjectInfo, String> {
        fn walk(
            project: &Project,
            relative: &Path,
            files: &mut Vec<String>,
            seen: &mut usize,
            limited: &mut bool,
            depth: usize,
        ) -> Result<(), String> {
            if depth > 20 || *seen >= 20000 || files.len() >= 1000 {
                *limited = true;
                return Ok(());
            }
            let _guards = guards(&project.root, relative, false)?;
            for entry in
                fs::read_dir(project.root.join(relative)).map_err(|_| "无法读取项目目录")?
            {
                *seen += 1;
                if *seen > 20000 || files.len() >= 1000 {
                    *limited = true;
                    break;
                }
                let Ok(entry) = entry else { continue };
                let name = entry.file_name().to_string_lossy().to_string();
                let lower = name.to_lowercase();
                if BLOCKED.contains(&lower.as_str()) || lower.starts_with(".env.") {
                    continue;
                }
                let path = relative.join(&name);
                let Ok(meta) = fs::symlink_metadata(entry.path()) else {
                    continue;
                };
                if reparse(&meta) {
                    continue;
                }
                if meta.is_dir() {
                    let _ = walk(project, &path, files, seen, limited, depth + 1);
                } else if meta.is_file() && meta.len() <= FILE_LIMIT {
                    let text = path.to_string_lossy().replace('\\', "/");
                    if allowed(&text).is_ok() {
                        files.push(text);
                    }
                }
            }
            Ok(())
        }
        let mut files = Vec::new();
        let mut limited = false;
        walk(self, Path::new(""), &mut files, &mut 0, &mut limited, 0)?;
        files.sort();
        Ok(ProjectInfo {
            project_id: self.id.clone(),
            name: self
                .root
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned(),
            directory: self
                .root
                .display()
                .to_string()
                .trim_start_matches(r"\\?\")
                .into(),
            files,
            limited,
        })
    }
    pub fn read(&self, path: &str) -> Result<TextFile, String> {
        allowed(path)?;
        let relative = Path::new(path);
        let _guards = guards(&self.root, relative.parent().unwrap(), false)?;
        let raw = bytes(&mut open_text(&self.root.join(relative), false)?)?;
        Ok(TextFile {
            path: path.into(),
            content: normalized(&raw)?,
            hash: hash(&raw),
        })
    }
    pub fn snapshot(&self, paths: &[String]) -> Result<Snapshot, String> {
        if paths.len() > 100 {
            return Err("最多选择 100 个文件".into());
        }
        let mut files = BTreeMap::new();
        let mut hashes = BTreeMap::new();
        let mut size = 0;
        for path in paths {
            let file = self.read(path)?;
            size += file.content.len();
            if size > 2_000_000 {
                return Err("所选快照超过 2 MB".into());
            }
            hashes.insert(path.clone(), file.hash);
            files.insert(path.clone(), file.content);
        }
        Ok(Snapshot { files, hashes })
    }
    pub fn apply(&mut self, operation_id: &str, edits: &[Edit]) -> Result<Receipt, String> {
        if edits.is_empty() || edits.len() > 5 || operation_id.len() > 100 {
            return Err("本次最多应用 5 个文件".into());
        }
        if let Some(receipt) = self.receipts.get(operation_id) {
            if receipt.files.iter().all(|f| {
                self.read(&f.path)
                    .map(|v| v.hash == f.hash)
                    .unwrap_or(false)
            }) {
                return Ok(receipt.clone());
            }
            return Err("已应用文件后来发生变化，不能重放旧补丁".into());
        }
        struct Prepared {
            path: String,
            target: PathBuf,
            file: Option<File>,
            before: Option<Vec<u8>>,
            after: Vec<u8>,
            _guards: Vec<File>,
        }
        let mut prepared = Vec::new();
        let mut seen = std::collections::BTreeSet::new();
        for edit in edits {
            allowed(&edit.path)?;
            if !seen.insert(&edit.path) {
                return Err("补丁路径重复".into());
            }
            let after = edit
                .after
                .as_ref()
                .ok_or("本版不通过 Agent 删除本机文件，请手动处理删除操作")?;
            let relative = Path::new(&edit.path);
            let pin = guards(&self.root, relative.parent().unwrap(), true)?;
            let target = self.root.join(relative);
            let (file, before) = match fs::symlink_metadata(&target) {
                Ok(_) => {
                    let mut file = open_text(&target, true)?;
                    let raw = bytes(&mut file)?;
                    if edit.before.as_deref() != Some(normalized(&raw)?.as_str())
                        || edit.expected_hash.as_deref() != Some(hash(&raw).as_str())
                    {
                        return Err(format!("{} 已在磁盘变化，请重新读取或生成补丁", edit.path));
                    }
                    (Some(file), Some(raw))
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    if edit.before.is_some() || edit.expected_hash.is_some() {
                        return Err("补丁依据的文件已被移除".into());
                    }
                    (None, None)
                }
                Err(_) => return Err("无法确认目标文件状态".into()),
            };
            let output = encoded(after, before.as_deref())?;
            prepared.push(Prepared {
                path: edit.path.clone(),
                target,
                file,
                before,
                after: output,
                _guards: pin,
            });
        }
        let backup = self.backups.join(&self.id).join(Uuid::new_v4().to_string());
        fs::create_dir_all(&backup).map_err(|_| "备份目录不可写，未修改代码")?;
        let mut manifest = Vec::new();
        for (index, item) in prepared.iter().enumerate() {
            if let Some(before) = &item.before {
                let mut f = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(backup.join(format!("{index}.bak")))
                    .map_err(|_| "无法备份原文件，未修改代码")?;
                f.write_all(before)
                    .and_then(|_| f.sync_all())
                    .map_err(|_| "无法保存备份，未修改代码")?;
            }
            manifest.push(serde_json::json!({"path":item.path,"backup":item.before.as_ref().map(|_|format!("{index}.bak")),"before_hash":item.before.as_ref().map(|v|hash(v)),"after_hash":hash(&item.after)}));
        }
        fs::write(
            backup.join("manifest.json"),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .map_err(|_| "无法保存备份清单，未修改代码")?;
        let mut written = 0;
        for index in 0..prepared.len() {
            let item = &mut prepared[index];
            let result = (|| -> std::io::Result<()> {
                if item.file.is_none() {
                    item.file = Some(options(true, false).create_new(true).open(&item.target)?);
                }
                let file = item.file.as_mut().unwrap();
                file.seek(SeekFrom::Start(0))?;
                file.write_all(&item.after)?;
                file.set_len(item.after.len() as u64)?;
                file.sync_all()
            })();
            if result.is_err() {
                // Recover best-effort in the same pinned directories; durable backups remain.
                for previous in &mut prepared[..=index] {
                    if let (Some(file), Some(before)) = (&mut previous.file, &previous.before) {
                        let _ = file.seek(SeekFrom::Start(0));
                        let _ = file.write_all(before);
                        let _ = file.set_len(before.len() as u64);
                        let _ = file.sync_all();
                    } else if previous.before.is_none() {
                        previous.file.take();
                        let _ = fs::remove_file(&previous.target);
                    }
                }
                return Err(format!(
                    "写入中断，已尝试恢复；请核对备份 {}",
                    backup.display()
                ));
            }
            written += 1;
        }
        let files = prepared
            .iter()
            .map(|p| TextFile {
                path: p.path.clone(),
                content: normalized(&p.after).unwrap(),
                hash: hash(&p.after),
            })
            .collect();
        let receipt = Receipt {
            operation_id: operation_id.into(),
            backup_directory: backup
                .display()
                .to_string()
                .trim_start_matches(r"\\?\")
                .into(),
            files,
        };
        self.receipts.insert(operation_id.into(), receipt.clone());
        let _ = written;
        Ok(receipt)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn project() -> Project {
        let root = std::env::temp_dir().join(format!("careerforge-local-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        Project::new(root.clone(), root.join("backups")).unwrap()
    }
    #[test]
    fn edit_patch_conflict_and_backups() {
        let mut p = project();
        fs::write(p.root.join("main.py"), b"\xef\xbb\xbfprint(1)\r\n").unwrap();
        let first = p.read("main.py").unwrap();
        assert_eq!(first.content, "print(1)\n");
        let e = Edit {
            path: "main.py".into(),
            before: Some(first.content),
            after: Some("print(2)\n".into()),
            expected_hash: Some(first.hash),
        };
        let receipt = p.apply("save", &[e.clone()]).unwrap();
        assert!(Path::new(&receipt.backup_directory).join("0.bak").is_file());
        assert_eq!(
            fs::read(p.root.join("main.py")).unwrap(),
            b"\xef\xbb\xbfprint(2)\r\n"
        );
        assert!(p.apply("stale", &[e]).is_err());
        let current = p.read("main.py").unwrap();
        let edits = [
            Edit {
                path: "main.py".into(),
                before: Some(current.content),
                after: Some("print(3)\n".into()),
                expected_hash: Some(current.hash),
            },
            Edit {
                path: "new.py".into(),
                before: None,
                after: Some("print('new')\n".into()),
                expected_hash: None,
            },
        ];
        p.apply("patch", &edits).unwrap();
        assert_eq!(p.read("main.py").unwrap().content, "print(3)\n");
        assert_eq!(p.apply("patch", &edits).unwrap().files.len(), 2);
    }
    #[test]
    fn deny_escape_private_files_and_links() {
        let mut p = project();
        for name in [
            "../outside.py",
            ".ENV",
            ".env.secret",
            ".git/config",
            "a/../../b.py",
            "C:/bad.py",
            "x\\bad.py",
            "con.py",
            "file.py:stream",
        ] {
            assert!(p.read(name).is_err());
        }
        let outside = p.root.with_extension("py");
        fs::write(&outside, "secret=1\n").unwrap();
        fs::hard_link(&outside, p.root.join("linked.py")).unwrap();
        #[cfg(windows)]
        assert!(p.read("linked.py").is_err());
        let original = fs::read(&outside).unwrap();
        let e = Edit {
            path: "linked.py".into(),
            before: Some("secret=1\n".into()),
            after: Some("secret=2\n".into()),
            expected_hash: Some(hash(&original)),
        };
        #[cfg(windows)]
        assert!(p.apply("link", &[e]).is_err());
        assert_eq!(fs::read(outside).unwrap(), original);
    }
}
