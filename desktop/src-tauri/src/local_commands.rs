use crate::local_project::{
    Edit, LocalProjects, Project, ProjectInfo, Receipt, Snapshot, TextFile,
};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
fn selected<'a>(state: &'a mut Option<Project>, id: &str) -> Result<&'a mut Project, String> {
    state
        .as_mut()
        .filter(|p| p.id == id)
        .ok_or("项目授权已失效，请重新选择目录".into())
}
#[tauri::command]
pub async fn local_project_pick(app: AppHandle) -> Result<Option<ProjectInfo>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let Some(path) = app
            .dialog()
            .file()
            .set_title("选择允许 CareerForge 读取和编辑的项目目录")
            .blocking_pick_folder()
        else {
            return Ok(None);
        };
        let root = path.into_path().map_err(|_| "请选择本机目录")?;
        let state = app.state::<LocalProjects>();
        let project = Project::new(root, state.backups.clone())?;
        let info = project.info()?;
        *state.selected.lock().map_err(|_| "项目状态不可用")? = Some(project);
        Ok(Some(info))
    })
    .await
    .map_err(|_| "目录选择中断".to_owned())?
}
#[tauri::command]
pub async fn local_project_tree(app: AppHandle, project_id: String) -> Result<ProjectInfo, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<LocalProjects>();
        let mut value = state.selected.lock().map_err(|_| "项目状态不可用")?;
        selected(&mut value, &project_id)?.info()
    })
    .await
    .map_err(|_| "读取目录中断".to_owned())?
}
#[tauri::command]
pub async fn local_project_read(
    app: AppHandle,
    project_id: String,
    path: String,
) -> Result<TextFile, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<LocalProjects>();
        let mut value = state.selected.lock().map_err(|_| "项目状态不可用")?;
        selected(&mut value, &project_id)?.read(&path)
    })
    .await
    .map_err(|_| "读取中断".to_owned())?
}
#[tauri::command]
pub async fn local_project_snapshot(
    app: AppHandle,
    project_id: String,
    paths: Vec<String>,
) -> Result<Snapshot, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<LocalProjects>();
        let mut value = state.selected.lock().map_err(|_| "项目状态不可用")?;
        selected(&mut value, &project_id)?.snapshot(&paths)
    })
    .await
    .map_err(|_| "快照读取中断".to_owned())?
}
#[tauri::command]
pub async fn local_project_write(
    app: AppHandle,
    project_id: String,
    edit: Edit,
) -> Result<Receipt, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<LocalProjects>();
        let mut value = state.selected.lock().map_err(|_| "项目状态不可用")?;
        selected(&mut value, &project_id)?.apply(&uuid::Uuid::new_v4().to_string(), &[edit])
    })
    .await
    .map_err(|_| "保存中断".to_owned())?
}
#[tauri::command]
pub async fn local_project_patch(
    app: AppHandle,
    project_id: String,
    patch_id: String,
    edits: Vec<Edit>,
) -> Result<Receipt, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if edits.is_empty()||edits.len()>5 {return Err("请选择有效补丁".into());}
        let state=app.state::<LocalProjects>();let mut value=state.selected.lock().map_err(|_|"项目状态不可用")?;
        let project=selected(&mut value,&project_id)?;let info=project.info()?;
        let message=format!("项目：{}\n目录：{}\n\n将写入：\n{}\n\n请先在工作台审阅修改前后正文。原文件会先备份；不会执行任何命令。",info.name,info.directory,edits.iter().map(|e|e.path.as_str()).collect::<Vec<_>>().join("\n"));
        if !app.dialog().message(message).title("确认写入本机 Agent 补丁").buttons(MessageDialogButtons::OkCancel).blocking_show() {return Err("已取消写入".into());}
        project.apply(&patch_id,&edits)
    }).await.map_err(|_|"补丁写入中断".to_owned())?
}
#[tauri::command]
pub fn local_project_close(app: AppHandle, project_id: String) -> Result<(), String> {
    let state = app.state::<LocalProjects>();
    let mut value = state.selected.lock().map_err(|_| "项目状态不可用")?;
    if value.as_ref().is_some_and(|p| p.id == project_id) {
        *value = None;
    }
    Ok(())
}

#[tauri::command]
pub fn local_project_capabilities() -> serde_json::Value {
    crate::services::record("local_ide.bridge", "ready");
    serde_json::json!({"selected_root_only":true,"host_commands":false})
}
