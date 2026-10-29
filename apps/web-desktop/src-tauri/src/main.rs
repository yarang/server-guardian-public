// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod export;
mod save_file;
mod token_store;

fn main() {
    tauri::Builder::default()
        // Used from Rust only (export_choose_root); no dialog permission is granted to the webview.
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            token_store::token_get,
            token_store::token_set,
            token_store::token_delete,
            save_file::save_download,
            export::export_get_root,
            export::export_choose_root,
            export::export_clear_root,
            export::export_save,
            export::export_auto_get,
            export::export_auto_set,
            export::export_auto_marks,
            export::export_auto_recent,
            export::export_auto_save,
            export::export_reveal
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
