fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_status",
            "start_services",
            "open_workspace",
        ]),
    ))
    .expect("Tauri build configuration");
}
