//! Saves a user-initiated text download (the thread result document) into the OS Downloads folder.
//!
//! WKWebView/WebView2 inside Tauri do not honour `<a download>` for blob URLs, so the frontend hands the
//! text to this command instead. The file name is reduced to a safe basename and an existing file is never
//! overwritten (`name (1).md`, `name (2).md`, ...).

use std::fs;
use std::path::PathBuf;
use tauri::Manager;

const MAX_BYTES: usize = 16 * 1024 * 1024;

fn safe_name(raw: &str) -> Result<String, String> {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = base
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' })
        .collect();
    let cleaned = cleaned.trim_start_matches('.').to_string();
    if cleaned.is_empty() || cleaned.len() > 120 {
        return Err("invalid file name".to_string());
    }
    if !cleaned.ends_with(".md") {
        return Err("only .md files can be saved".to_string());
    }
    Ok(cleaned)
}

fn free_path(dir: &PathBuf, name: &str) -> PathBuf {
    let first = dir.join(name);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = name.rsplit_once('.').unwrap_or((name, ""));
    for n in 1..1000 {
        let candidate = dir.join(format!("{stem} ({n}).{ext}"));
        if !candidate.exists() {
            return candidate;
        }
    }
    first
}

/// Writes `contents` to `<Downloads>/<file_name>` and returns the final path.
#[tauri::command]
pub fn save_download(app: tauri::AppHandle, file_name: String, contents: String) -> Result<String, String> {
    if contents.len() > MAX_BYTES {
        return Err("file too large".to_string());
    }
    let name = safe_name(&file_name)?;
    let dir = app.path().download_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = free_path(&dir, &name);
    fs::write(&path, contents).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().into_owned())
}

#[cfg(test)]
mod tests {
    use super::safe_name;

    #[test]
    fn keeps_only_a_safe_markdown_basename() {
        assert_eq!(safe_name("thread_ab1.md").unwrap(), "thread_ab1.md");
        assert_eq!(safe_name("../../etc/thread x.md").unwrap(), "thread_x.md");
        assert!(safe_name("x.sh").is_err());
        assert!(safe_name("..").is_err());
        assert!(safe_name("").is_err());
    }
}
