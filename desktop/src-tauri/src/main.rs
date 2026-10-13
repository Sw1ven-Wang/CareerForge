#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod local_commands;
mod local_project;
mod services;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

static STARTING: AtomicBool = AtomicBool::new(false);

#[tauri::command]
async fn desktop_status() -> Result<services::DesktopStatus, String> {
    tauri::async_runtime::spawn_blocking(services::status)
        .await
        .map_err(|_| "状态检查中断".to_owned())
}

#[tauri::command]
async fn start_services() -> Result<String, String> {
    if STARTING.swap(true, Ordering::SeqCst) {
        return Err("服务正在启动，请稍候。".into());
    }
    services::record("services.start", "requested");
    let result = tauri::async_runtime::spawn_blocking(services::start).await;
    services::record(
        "services.start",
        if matches!(&result, Ok(Ok(_))) {
            "completed"
        } else {
            "not_ready"
        },
    );
    STARTING.store(false, Ordering::SeqCst);
    result.map_err(|_| "启动任务中断".to_owned())?
}

#[tauri::command]
async fn open_workspace(app: tauri::AppHandle) -> Result<(), String> {
    let ready = tauri::async_runtime::spawn_blocking(services::web_ready)
        .await
        .map_err(|_| "连接检查中断")?;
    if !ready {
        return Err("工作台尚未就绪，请先启动服务。".into());
    }
    if let Some(window) = app.get_webview_window("workspace") {
        window.show().map_err(|_| "无法显示工作台")?;
        window.set_focus().map_err(|_| "无法聚焦工作台")?;
    } else {
        WebviewWindowBuilder::new(
            &app,
            "workspace",
            WebviewUrl::External("http://127.0.0.1:8080/".parse().unwrap()),
        )
        .title("CareerForge · 学习工作台")
        .inner_size(1440.0, 940.0)
        .min_inner_size(1000.0, 680.0)
        .on_navigation(|url| {
            url.scheme() == "http"
                && url.host_str() == Some("127.0.0.1")
                && url.port() == Some(8080)
        })
        .build()
        .map_err(|_| "无法创建工作台窗口")?;
    }
    services::record("workspace.open", "completed");
    Ok(())
}

fn main() {
    // A single explicit read-only diagnostic, also usable when the WebView cannot start.
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--diagnose") {
        if let Some(path) = args.get(2) {
            let result = serde_json::to_vec_pretty(&services::status()).unwrap();
            let _ = std::fs::write(path, result);
        }
        return;
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("manager") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![
            desktop_status,
            start_services,
            open_workspace,
            local_commands::local_project_pick,
            local_commands::local_project_tree,
            local_commands::local_project_read,
            local_commands::local_project_snapshot,
            local_commands::local_project_write,
            local_commands::local_project_patch,
            local_commands::local_project_close,
            local_commands::local_project_capabilities
        ])
        .on_page_load(|webview, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                services::record("window.loaded", webview.label());
            }
        })
        .setup(|app| {
            services::record("desktop.start", "manager");
            app.manage(local_project::LocalProjects::new(
                app.path().app_data_dir()?.join("code-backups"),
            ));
            let menu = tauri::menu::MenuBuilder::new(app)
                .text("status", "服务状态")
                .text("workspace", "学习工作台")
                .separator()
                .quit()
                .build()?;
            app.set_menu(menu)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "manager"
                    && window
                        .app_handle()
                        .get_webview_window("workspace")
                        .is_some()
                {
                    api.prevent_close();
                    let _ = window.hide();
                } else if window.label() == "workspace" {
                    if let Some(manager) = window.app_handle().get_webview_window("manager") {
                        let _ = manager.show();
                    }
                }
            }
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "status" => {
                if let Some(window) = app.get_webview_window("manager") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "workspace" => {
                let handle = app.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = open_workspace(handle).await;
                });
            }
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("CareerForge desktop runtime");
}
