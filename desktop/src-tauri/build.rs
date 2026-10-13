fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_status",
            "start_services",
            "open_workspace",
            "local_project_pick",
            "local_project_tree",
            "local_project_read",
            "local_project_snapshot",
            "local_project_write",
            "local_project_patch",
            "local_project_close",
            "local_project_capabilities",
        ]),
    ))
    .expect("Tauri build configuration");
}
