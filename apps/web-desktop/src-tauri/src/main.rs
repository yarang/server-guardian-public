// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod save_file;
mod token_store;

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            token_store::token_get,
            token_store::token_set,
            token_store::token_delete,
            save_file::save_download
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
