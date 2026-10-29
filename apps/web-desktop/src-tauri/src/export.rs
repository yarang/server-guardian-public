//! Saves thread results (reports, design artifacts) into an export folder the operator picked once.
//!
//! Design: `docs/design/desktop-export.md`. The webview never names a path: the folder dialog is opened from here, the
//! root lives in `export.json` in the app config directory, and `export_save` takes only a project id, a thread id and
//! relative file names that are validated component by component before anything is written.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

const MAX_FILE_BYTES: usize = 16 * 1024 * 1024;
const MAX_CALL_BYTES: usize = 64 * 1024 * 1024;
const MAX_FILES: usize = 500;
const MAX_DEPTH: usize = 3;
const EXTENSIONS: [&str; 3] = ["md", "mmd", "svg"];

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportFile {
    rel_path: String,
    contents: String,
}

#[derive(Serialize, Debug, PartialEq, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    Written,
    Skipped,
    Renamed,
    /// Automatic export only: the file still held exactly what Guardian wrote last time, so it was replaced in place.
    Updated,
}

#[derive(Serialize, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct FileResult {
    rel_path: String,
    outcome: Outcome,
    /// Path of the file that holds (or already held) the contents, relative to the export root.
    saved_as: String,
}

// ---- validation -------------------------------------------------------------------------------------------------

fn is_reserved_windows_name(component: &str) -> bool {
    let base = component.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL") {
        return true;
    }
    base.len() == 4
        && (base.starts_with("COM") || base.starts_with("LPT"))
        && base.as_bytes()[3].is_ascii_digit()
}

/// `^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$`, no trailing dot, not a Windows device name.
fn valid_component(c: &str) -> bool {
    let mut chars = c.chars();
    let first_ok = chars.next().is_some_and(|f| f.is_ascii_alphanumeric());
    first_ok
        && c.len() <= 81
        && c.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
        && !c.ends_with('.')
        && !is_reserved_windows_name(c)
}

/// `^[a-z0-9][a-z0-9_-]{0,63}$` (the hub's project id pattern).
fn valid_project(p: &str) -> bool {
    let mut chars = p.chars();
    let first_ok = chars.next().is_some_and(|f| f.is_ascii_lowercase() || f.is_ascii_digit());
    first_ok
        && p.len() <= 64
        && p.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '_' | '-'))
        && !is_reserved_windows_name(p)
}

/// `^thread_[A-Za-z0-9_]{1,80}$` (the hub's thread id pattern).
fn valid_thread(t: &str) -> bool {
    t.strip_prefix("thread_")
        .is_some_and(|rest| (1..=80).contains(&rest.len()) && rest.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))
}

/// Splits a relative path into validated components; the last one must carry an allowed extension.
fn parse_rel_path(rel: &str) -> Result<Vec<&str>, String> {
    let parts: Vec<&str> = rel.split('/').collect();
    if parts.len() > MAX_DEPTH {
        return Err(format!("path too deep: {rel}"));
    }
    if parts.iter().any(|c| !valid_component(c)) {
        return Err(format!("invalid path: {rel}"));
    }
    let name = parts[parts.len() - 1];
    let ext_ok = name
        .rsplit_once('.')
        .is_some_and(|(_, ext)| EXTENSIONS.contains(&ext.to_ascii_lowercase().as_str()));
    if !ext_ok {
        return Err(format!("only .md, .mmd and .svg files can be exported: {rel}"));
    }
    Ok(parts)
}

// ---- writing ----------------------------------------------------------------------------------------------------

/// Creates `root/<components...>` one directory at a time and refuses symlinks and non-directories on the way.
fn ensure_dir(root: &Path, components: &[&str]) -> Result<PathBuf, String> {
    let mut cur = root.to_path_buf();
    for c in components {
        cur.push(c);
        match fs::symlink_metadata(&cur) {
            Ok(m) if m.file_type().is_symlink() => return Err(format!("symbolic link refused: {c}")),
            Ok(m) if m.is_dir() => {}
            Ok(_) => return Err(format!("not a directory: {c}")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => fs::create_dir(&cur).map_err(|e| e.to_string())?,
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(cur)
}

/// `name` is `stem (N).ext` for the given (lower case) stem and extension.
fn is_suffixed_variant(name_lc: &str, stem_lc: &str, ext_lc: &str) -> bool {
    name_lc
        .strip_prefix(&format!("{stem_lc} ("))
        .and_then(|rest| rest.strip_suffix(&format!(").{ext_lc}")))
        .is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
}

/// FNV-1a, 64 bit, as 16 hex digits. Only used to notice that a file is still what Guardian wrote, so a stable and
/// dependency-free hash is enough; it is persisted in `export.json`, which rules out `DefaultHasher`.
fn content_hash(bytes: &[u8]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in bytes {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

struct Placed {
    outcome: Outcome,
    name: String,
    /// Set when the base name now holds `contents`: the hash to remember for a later in-place update.
    base_hash: Option<String>,
}

/// Writes `contents` as `dir/name` without overwriting anything the operator may have touched: identical bytes under
/// the name or any `name (N)` variant (compared case-insensitively) are skipped, anything else takes the next free
/// suffix. With `update` and a `recorded` hash, a base file that still hashes to `recorded` (so it is exactly what
/// Guardian wrote earlier) is replaced in place instead.
fn place_file(dir: &Path, name: &str, contents: &[u8], recorded: Option<&str>, update: bool) -> Result<Placed, String> {
    let (stem, ext) = name.rsplit_once('.').ok_or("file name without extension")?;
    let (stem_lc, ext_lc) = (stem.to_lowercase(), ext.to_lowercase());
    let base_lc = format!("{stem_lc}.{ext_lc}");

    let mut taken: HashSet<String> = HashSet::new();
    let mut variants: Vec<(String, PathBuf)> = Vec::new();
    for entry in fs::read_dir(dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let file_name = entry.file_name().to_string_lossy().into_owned();
        let lc = file_name.to_lowercase();
        if lc == base_lc || is_suffixed_variant(&lc, &stem_lc, &ext_lc) {
            taken.insert(lc);
            variants.push((file_name, entry.path()));
        }
    }
    variants.sort();
    for (file_name, path) in &variants {
        let meta = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
        if meta.file_type().is_symlink() {
            return Err(format!("symbolic link refused: {file_name}"));
        }
        if !meta.is_file() {
            return Err(format!("exists and is not a file: {file_name}"));
        }
        let on_disk = fs::read(path).map_err(|e| e.to_string())?;
        let is_base = file_name.to_lowercase() == base_lc;
        if on_disk == contents {
            let base_hash = is_base.then(|| content_hash(contents));
            return Ok(Placed { outcome: Outcome::Skipped, name: file_name.clone(), base_hash });
        }
        if update && is_base && recorded == Some(content_hash(&on_disk).as_str()) {
            write_atomic(dir, file_name, contents)?;
            return Ok(Placed { outcome: Outcome::Updated, name: file_name.clone(), base_hash: Some(content_hash(contents)) });
        }
    }

    let (outcome, final_name) = if taken.is_empty() {
        (Outcome::Written, name.to_string())
    } else {
        let free = (1..10_000)
            .map(|n| format!("{stem} ({n}).{ext}"))
            .find(|candidate| !taken.contains(&candidate.to_lowercase()))
            .ok_or("too many copies of this file")?;
        (Outcome::Renamed, free)
    };
    write_atomic(dir, &final_name, contents)?;
    let base_hash = (outcome == Outcome::Written).then(|| content_hash(contents));
    Ok(Placed { outcome, name: final_name, base_hash })
}

/// Writes to a fresh temporary name in the same folder, then renames, so a half-written result never appears.
fn write_atomic(dir: &Path, final_name: &str, contents: &[u8]) -> Result<(), String> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp = dir.join(format!(".{final_name}.{}.{nanos}.tmp", std::process::id()));
    let result = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&tmp)
        .and_then(|mut f| f.write_all(contents).and_then(|_| f.sync_all()))
        .and_then(|_| fs::rename(&tmp, dir.join(final_name)));
    if let Err(e) = result {
        let _ = fs::remove_file(&tmp);
        return Err(e.to_string());
    }
    Ok(())
}

/// Hash of what Guardian last wrote at a file's base name, keyed `<project>/<thread>/<relPath>`.
pub type Ledger = BTreeMap<String, String>;

/// Validates every file first, then writes them under `root/<project>/<thread>/`. Nothing is written when any file
/// is rejected. `ledger` remembers the hash of each file written at its base name; with `update` a file that still
/// matches its ledger entry is replaced in place (automatic export), otherwise nothing is ever overwritten.
pub fn save_files(
    root: &Path,
    project: &str,
    thread: &str,
    files: &[ExportFile],
    ledger: &mut Ledger,
    update: bool,
) -> Result<Vec<FileResult>, String> {
    if files.is_empty() || files.len() > MAX_FILES {
        return Err(format!("between 1 and {MAX_FILES} files per call"));
    }
    if !valid_project(project) || !valid_thread(thread) {
        return Err("invalid project or thread id".to_string());
    }
    let mut total = 0usize;
    let mut parsed = Vec::with_capacity(files.len());
    for f in files {
        if f.contents.len() > MAX_FILE_BYTES {
            return Err(format!("file too large: {}", f.rel_path));
        }
        total += f.contents.len();
        parsed.push(parse_rel_path(&f.rel_path)?);
    }
    if total > MAX_CALL_BYTES {
        return Err("too much data in one call".to_string());
    }

    let root = root.canonicalize().map_err(|_| "the export folder is not available".to_string())?;
    if !root.is_dir() {
        return Err("the export folder is not available".to_string());
    }
    let mut out = Vec::with_capacity(files.len());
    for (f, parts) in files.iter().zip(&parsed) {
        let (name, dirs) = parts.split_last().expect("parse_rel_path returns at least one component");
        let mut components = vec![project, thread];
        components.extend(dirs);
        let dir = ensure_dir(&root, &components)?;
        if !dir.starts_with(&root) {
            return Err("path escapes the export folder".to_string());
        }
        let key = format!("{project}/{thread}/{}", f.rel_path);
        let placed = place_file(&dir, name, f.contents.as_bytes(), ledger.get(&key).map(String::as_str), update)?;
        if let Some(hash) = placed.base_hash {
            ledger.insert(key, hash);
        }
        let mut saved = components.join("/");
        saved.push('/');
        saved.push_str(&placed.name);
        out.push(FileResult { rel_path: f.rel_path.clone(), outcome: placed.outcome, saved_as: saved });
    }
    Ok(out)
}

// ---- stored root ------------------------------------------------------------------------------------------------

/// One automatic write, for the list in Settings.
#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct Recent {
    at_ms: u64,
    project: String,
    thread: String,
    written: u32,
    skipped: u32,
    renamed: u32,
    updated: u32,
}

const MAX_RECENT: usize = 20;

/// `export.json`. Unknown keys are kept so later phases can add fields without losing them.
#[derive(Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct Config {
    root: Option<String>,
    /// Automatic export switch (docs/design/desktop-export.md 3.7) and when it was turned on (unix ms).
    #[serde(default)]
    auto: bool,
    #[serde(default)]
    auto_since_ms: u64,
    /// What Guardian last wrote at each base name; see [`Ledger`].
    #[serde(default)]
    hashes: Ledger,
    /// `<project>/<thread>` -> the result marker (its `updated_at`) that was last exported automatically.
    #[serde(default)]
    marks: BTreeMap<String, String>,
    #[serde(default)]
    recent: Vec<Recent>,
    #[serde(flatten)]
    rest: serde_json::Map<String, serde_json::Value>,
}

fn config_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("export.json"))
}

fn load_config(app: &tauri::AppHandle) -> Config {
    config_path(app)
        .ok()
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn store_config(app: &tauri::AppHandle, cfg: &Config) -> Result<(), String> {
    let path = config_path(app)?;
    let dir = path.parent().ok_or("no config directory")?;
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let text = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    let tmp = dir.join("export.json.tmp");
    fs::write(&tmp, text).map_err(|e| e.to_string())?;
    fs::rename(&tmp, &path).map_err(|e| e.to_string())
}

/// The stored root, only while it still exists as a directory.
fn current_root(app: &tauri::AppHandle) -> Option<PathBuf> {
    load_config(app).root.map(PathBuf::from).filter(|p| p.is_dir())
}

// ---- commands ---------------------------------------------------------------------------------------------------

#[tauri::command]
pub fn export_get_root(app: tauri::AppHandle) -> Option<String> {
    current_root(&app).map(|p| p.to_string_lossy().into_owned())
}

/// Opens the native folder dialog from Rust, stores the choice and returns its display path; `None` when cancelled.
#[tauri::command]
pub async fn export_choose_root(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let Some(picked) = app.dialog().file().blocking_pick_folder() else {
        return Ok(None);
    };
    let path = picked.into_path().map_err(|e| e.to_string())?;
    let root = path.canonicalize().map_err(|e| e.to_string())?;
    if root.parent().is_none() {
        return Err("choose a folder, not the top of a drive".to_string());
    }
    let _guard = lock();
    let mut cfg = load_config(&app);
    let new_root = root.to_string_lossy().into_owned();
    if cfg.root.as_deref() != Some(new_root.as_str()) {
        // Another folder holds none of what was exported, so the bookkeeping about it no longer applies.
        cfg.hashes.clear();
        cfg.marks.clear();
    }
    cfg.root = Some(new_root);
    store_config(&app, &cfg)?;
    Ok(Some(root.to_string_lossy().into_owned()))
}

/// Forgets the root; the folder and the files in it stay.
#[tauri::command]
pub fn export_clear_root(app: tauri::AppHandle) -> Result<(), String> {
    let _guard = lock();
    let mut cfg = load_config(&app);
    cfg.root = None;
    cfg.auto = false;
    cfg.hashes.clear();
    cfg.marks.clear();
    store_config(&app, &cfg)
}

/// Serializes every read-modify-write of `export.json` and of the export folder (manual and automatic saves).
static EXPORT_LOCK: Mutex<()> = Mutex::new(());

fn lock() -> std::sync::MutexGuard<'static, ()> {
    EXPORT_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Manual save: never overwrites; remembers what it wrote so a later automatic export can update it in place.
#[tauri::command]
pub async fn export_save(
    app: tauri::AppHandle,
    project: String,
    thread: String,
    files: Vec<ExportFile>,
) -> Result<Vec<FileResult>, String> {
    let _guard = lock();
    let root = current_root(&app).ok_or("no export folder is set")?;
    let mut cfg = load_config(&app);
    let result = save_files(&root, &project, &thread, &files, &mut cfg.hashes, false)?;
    store_config(&app, &cfg)?;
    Ok(result)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AutoState {
    enabled: bool,
    since_ms: u64,
}

#[tauri::command]
pub fn export_auto_get(app: tauri::AppHandle) -> AutoState {
    let cfg = load_config(&app);
    AutoState { enabled: cfg.auto && current_root(&app).is_some(), since_ms: cfg.auto_since_ms }
}

/// Turns automatic export on or off. Turning it on needs a root and starts the clock: only results that finish after
/// this moment are exported, nothing is back-filled.
#[tauri::command]
pub fn export_auto_set(app: tauri::AppHandle, enabled: bool) -> Result<AutoState, String> {
    let _guard = lock();
    let mut cfg = load_config(&app);
    if enabled {
        if current_root(&app).is_none() {
            return Err("choose an export folder first".to_string());
        }
        if !cfg.auto {
            cfg.auto_since_ms = now_ms();
        }
    }
    cfg.auto = enabled;
    store_config(&app, &cfg)?;
    Ok(AutoState { enabled, since_ms: cfg.auto_since_ms })
}

/// `<thread> -> marker` of one project's automatic exports, so the dashboard decides without touching the folder.
#[tauri::command]
pub fn export_auto_marks(app: tauri::AppHandle, project: String) -> BTreeMap<String, String> {
    let prefix = format!("{project}/");
    load_config(&app)
        .marks
        .into_iter()
        .filter_map(|(k, v)| k.strip_prefix(&prefix).map(|t| (t.to_string(), v)))
        .collect()
}

#[tauri::command]
pub fn export_auto_recent(app: tauri::AppHandle) -> Vec<Recent> {
    load_config(&app).recent
}

/// Automatic save of one thread: like `export_save`, but a report that is still exactly what Guardian wrote earlier
/// is updated in place. Records `marker` for the thread and adds the write to the recent list.
#[tauri::command]
pub async fn export_auto_save(
    app: tauri::AppHandle,
    project: String,
    thread: String,
    marker: String,
    files: Vec<ExportFile>,
) -> Result<Vec<FileResult>, String> {
    if marker.is_empty() || marker.len() > 64 {
        return Err("invalid result marker".to_string());
    }
    let _guard = lock();
    let mut cfg = load_config(&app);
    if !cfg.auto {
        return Err("automatic export is off".to_string());
    }
    let root = current_root(&app).ok_or("no export folder is set")?;
    let result = save_files(&root, &project, &thread, &files, &mut cfg.hashes, true)?;
    let count = |o: Outcome| result.iter().filter(|r| r.outcome == o).count() as u32;
    let entry = Recent {
        at_ms: now_ms(),
        project: project.clone(),
        thread: thread.clone(),
        written: count(Outcome::Written),
        skipped: count(Outcome::Skipped),
        renamed: count(Outcome::Renamed),
        updated: count(Outcome::Updated),
    };
    cfg.marks.insert(format!("{project}/{thread}"), marker);
    // Nothing new on disk (everything skipped) is not worth a line in the list.
    if entry.written + entry.renamed + entry.updated > 0 {
        cfg.recent.insert(0, entry);
        cfg.recent.truncate(MAX_RECENT);
    }
    store_config(&app, &cfg)?;
    Ok(result)
}

/// Opens the export root (never another path) in the OS file manager.
#[tauri::command]
pub fn export_reveal(app: tauri::AppHandle) -> Result<(), String> {
    let root = current_root(&app).ok_or("no export folder is set")?;
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(target_os = "windows") {
        "explorer"
    } else {
        "xdg-open"
    };
    std::process::Command::new(opener).arg(&root).spawn().map(|_| ()).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            static N: AtomicUsize = AtomicUsize::new(0);
            let dir = std::env::temp_dir().join(format!(
                "sg-export-test-{}-{}",
                std::process::id(),
                N.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&dir).unwrap();
            Scratch(dir)
        }
        fn root(&self) -> PathBuf {
            self.0.join("root")
        }
        fn with_root(self) -> Self {
            fs::create_dir_all(self.root()).unwrap();
            self
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const T: &str = "thread_ab12";

    fn file(rel: &str, contents: &str) -> ExportFile {
        ExportFile { rel_path: rel.to_string(), contents: contents.to_string() }
    }
    fn save(s: &Scratch, files: &[ExportFile]) -> Result<Vec<FileResult>, String> {
        save_files(&s.root(), "my-service", T, files, &mut Ledger::new(), false)
    }
    /// Manual and automatic saves share one ledger, as they share `export.json`.
    fn save_with(s: &Scratch, ledger: &mut Ledger, update: bool, files: &[ExportFile]) -> Vec<FileResult> {
        save_files(&s.root(), "my-service", T, files, ledger, update).unwrap()
    }
    fn outcomes(r: &[FileResult]) -> Vec<Outcome> {
        r.iter().map(|f| f.outcome).collect()
    }

    #[test]
    fn writes_the_fixed_layout() {
        let s = Scratch::new().with_root();
        let r = save(&s, &[file("report.md", "# r"), file("v1/diagram.mmd", "graph TD"), file("v1/logo.svg", "<svg/>")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Written; 3]);
        assert_eq!(r[1].saved_as, "my-service/thread_ab12/v1/diagram.mmd");
        let base = s.root().join("my-service").join(T);
        assert_eq!(fs::read_to_string(base.join("report.md")).unwrap(), "# r");
        assert_eq!(fs::read_to_string(base.join("v1").join("logo.svg")).unwrap(), "<svg/>");
    }

    #[test]
    fn rejects_traversal_absolute_and_malformed_paths() {
        let s = Scratch::new().with_root();
        for bad in [
            "../x.md", "a/../x.md", "/etc/x.md", "a//x.md", "", "x.md/", ".hidden.md", "a\\b.md", "x.md.", "a b.md",
            "é.md", "-x.md",
        ] {
            assert!(save(&s, &[file(bad, "x")]).is_err(), "accepted {bad:?}");
        }
        assert!(fs::read_dir(s.root()).unwrap().next().is_none(), "something was written");
    }

    #[test]
    fn limits_depth_and_extension() {
        let s = Scratch::new().with_root();
        assert!(save(&s, &[file("a/b/c.md", "x")]).is_ok());
        assert!(save(&s, &[file("a/b/c/d.md", "x")]).is_err());
        for bad in ["x.sh", "x", "x.md.exe", "x.html", "v1/x.png"] {
            assert!(save(&s, &[file(bad, "x")]).is_err(), "accepted {bad:?}");
        }
        assert!(save(&s, &[file("X.MD", "x")]).is_ok(), "extensions are case-insensitive");
    }

    #[test]
    fn rejects_windows_reserved_names_by_stem() {
        let s = Scratch::new().with_root();
        for bad in ["CON.md", "nul.svg", "Com1.md", "lpt9.mmd", "aux.tar.md", "v1/PRN.md", "con/x.md"] {
            assert!(save(&s, &[file(bad, "x")]).is_err(), "accepted {bad:?}");
        }
        assert!(save_files(&s.root(), "con", T, &[file("x.md", "x")], &mut Ledger::new(), false).is_err());
        assert!(save(&s, &[file("console.md", "x")]).is_ok());
    }

    #[test]
    fn rejects_bad_project_and_thread_ids() {
        let s = Scratch::new().with_root();
        let f = [file("report.md", "x")];
        for bad in ["", "../x", "My-Service", "-x", "a/b", "a b", &"a".repeat(65)] {
            assert!(save_files(&s.root(), bad, T, &f, &mut Ledger::new(), false).is_err(), "accepted project {bad:?}");
        }
        for bad in ["thread_", "thread", "ab12", "thread_a-b", "thread_../x", "../thread_x", &format!("thread_{}", "a".repeat(81))] {
            assert!(save_files(&s.root(), "p", bad, &f, &mut Ledger::new(), false).is_err(), "accepted thread {bad:?}");
        }
        assert!(save_files(&s.root(), &"a".repeat(64), &format!("thread_{}", "A".repeat(80)), &f, &mut Ledger::new(), false).is_ok());
    }

    #[test]
    fn rejects_the_whole_call_when_one_file_is_bad() {
        let s = Scratch::new().with_root();
        assert!(save(&s, &[file("report.md", "ok"), file("../bad.md", "x")]).is_err());
        assert!(fs::read_dir(s.root()).unwrap().next().is_none());
    }

    #[test]
    fn identical_reexport_is_skipped() {
        let s = Scratch::new().with_root();
        save(&s, &[file("report.md", "same")]).unwrap();
        let r = save(&s, &[file("report.md", "same")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Skipped]);
        let dir = s.root().join("my-service").join(T);
        assert_eq!(fs::read_dir(dir).unwrap().count(), 1);
    }

    #[test]
    fn changed_reexport_gets_a_suffix_and_never_overwrites() {
        let s = Scratch::new().with_root();
        save(&s, &[file("report.md", "one")]).unwrap();
        let r = save(&s, &[file("report.md", "two")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
        assert_eq!(r[0].saved_as, "my-service/thread_ab12/report (1).md");
        let dir = s.root().join("my-service").join(T);
        assert_eq!(fs::read_to_string(dir.join("report.md")).unwrap(), "one");
        assert_eq!(fs::read_to_string(dir.join("report (1).md")).unwrap(), "two");
    }

    #[test]
    fn a_suffixed_copy_with_the_same_bytes_counts_as_exported() {
        let s = Scratch::new().with_root();
        save(&s, &[file("report.md", "one")]).unwrap();
        save(&s, &[file("report.md", "two")]).unwrap();
        // "two" lives in "report (1).md" now; exporting it again must not create "report (2).md".
        let r = save(&s, &[file("report.md", "two")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Skipped]);
        assert_eq!(r[0].saved_as, "my-service/thread_ab12/report (1).md");
        // A third, different version takes the next free suffix.
        let r = save(&s, &[file("report.md", "three")]).unwrap();
        assert_eq!(r[0].saved_as, "my-service/thread_ab12/report (2).md");
        let dir = s.root().join("my-service").join(T);
        assert_eq!(fs::read_dir(dir).unwrap().count(), 3);
    }

    #[test]
    fn names_that_differ_only_in_case_are_one_file() {
        let s = Scratch::new().with_root();
        save(&s, &[file("Report.md", "one")]).unwrap();
        let r = save(&s, &[file("report.md", "two")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
        let r = save(&s, &[file("REPORT.md", "one")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Skipped]);
    }

    #[test]
    fn duplicate_names_in_one_call_are_stored_once() {
        let s = Scratch::new().with_root();
        let r = save(&s, &[file("report.md", "a"), file("report.md", "a"), file("report.md", "b")]).unwrap();
        assert_eq!(outcomes(&r), vec![Outcome::Written, Outcome::Skipped, Outcome::Renamed]);
    }

    #[test]
    fn leaves_no_temporary_files_behind() {
        let s = Scratch::new().with_root();
        save(&s, &[file("report.md", "x"), file("v1/a.md", "y")]).unwrap();
        let mut stack = vec![s.root()];
        while let Some(d) = stack.pop() {
            for e in fs::read_dir(d).unwrap() {
                let p = e.unwrap().path();
                assert!(!p.to_string_lossy().ends_with(".tmp"), "left {p:?}");
                if p.is_dir() {
                    stack.push(p);
                }
            }
        }
    }

    #[test]
    fn a_missing_root_is_an_error_and_is_not_created() {
        let s = Scratch::new();
        assert!(save(&s, &[file("report.md", "x")]).is_err());
        assert!(!s.root().exists());
    }

    #[cfg(unix)]
    #[test]
    fn refuses_symlinks_below_the_root() {
        use std::os::unix::fs::symlink;
        let s = Scratch::new().with_root();
        let outside = s.0.join("outside");
        fs::create_dir_all(&outside).unwrap();

        // a project folder that is a link to somewhere else
        symlink(&outside, s.root().join("my-service")).unwrap();
        assert!(save(&s, &[file("report.md", "x")]).is_err());
        assert!(fs::read_dir(&outside).unwrap().next().is_none());
        fs::remove_file(s.root().join("my-service")).unwrap();

        // a version folder that is a link
        let thread_dir = s.root().join("my-service").join(T);
        fs::create_dir_all(&thread_dir).unwrap();
        symlink(&outside, thread_dir.join("v1")).unwrap();
        assert!(save(&s, &[file("v1/a.md", "x")]).is_err());

        // the target file itself is a link
        symlink(outside.join("target.md"), thread_dir.join("report.md")).unwrap();
        assert!(save(&s, &[file("report.md", "x")]).is_err());
        assert!(fs::read_dir(&outside).unwrap().next().is_none());
    }

    #[cfg(unix)]
    #[test]
    fn a_root_that_is_itself_a_link_is_resolved_not_rejected() {
        use std::os::unix::fs::symlink;
        let s = Scratch::new();
        let real = s.0.join("real");
        fs::create_dir_all(&real).unwrap();
        symlink(&real, s.root()).unwrap();
        save(&s, &[file("report.md", "x")]).unwrap();
        assert!(real.join("my-service").join(T).join("report.md").is_file());
    }

    #[test]
    fn enforces_the_size_and_count_caps() {
        let s = Scratch::new().with_root();
        let big = "x".repeat(MAX_FILE_BYTES + 1);
        assert!(save(&s, &[file("report.md", &big)]).is_err());
        let max = "x".repeat(MAX_FILE_BYTES);
        let five = [
            file("a.md", &max),
            file("b.md", &max),
            file("c.md", &max),
            file("d.md", &max),
            file("e.md", "x"),
        ];
        assert!(save(&s, &five).is_err(), "64 MiB + 1 byte in one call");
        let many: Vec<ExportFile> = (0..=MAX_FILES).map(|i| file(&format!("f{i}.md"), "x")).collect();
        assert!(save(&s, &many).is_err());
        assert!(save(&s, &[]).is_err());
        assert!(fs::read_dir(s.root()).unwrap().next().is_none());
    }

    #[test]
    fn automatic_export_updates_a_report_it_wrote_and_nobody_touched() {
        let s = Scratch::new().with_root();
        let mut ledger = Ledger::new();
        let r = save_with(&s, &mut ledger, true, &[file("report.md", "round 1")]);
        assert_eq!(outcomes(&r), vec![Outcome::Written]);
        let r = save_with(&s, &mut ledger, true, &[file("report.md", "round 2")]);
        assert_eq!(outcomes(&r), vec![Outcome::Updated]);
        assert_eq!(r[0].saved_as, "my-service/thread_ab12/report.md");
        let dir = s.root().join("my-service").join(T);
        assert_eq!(fs::read_to_string(dir.join("report.md")).unwrap(), "round 2");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        // And the new content is what the ledger remembers, so round 3 updates again.
        let r = save_with(&s, &mut ledger, true, &[file("report.md", "round 3")]);
        assert_eq!(outcomes(&r), vec![Outcome::Updated]);
    }

    #[test]
    fn a_report_the_operator_edited_is_kept_and_the_new_one_gets_a_suffix() {
        let s = Scratch::new().with_root();
        let mut ledger = Ledger::new();
        save_with(&s, &mut ledger, true, &[file("report.md", "round 1")]);
        let dir = s.root().join("my-service").join(T);
        fs::write(dir.join("report.md"), "round 1 with my notes").unwrap();
        let r = save_with(&s, &mut ledger, true, &[file("report.md", "round 2")]);
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
        assert_eq!(fs::read_to_string(dir.join("report.md")).unwrap(), "round 1 with my notes");
        assert_eq!(fs::read_to_string(dir.join("report (1).md")).unwrap(), "round 2");
    }

    #[test]
    fn manual_export_never_updates_in_place_but_feeds_the_ledger() {
        let s = Scratch::new().with_root();
        let mut ledger = Ledger::new();
        save_with(&s, &mut ledger, false, &[file("report.md", "one")]);
        let r = save_with(&s, &mut ledger, false, &[file("report.md", "two")]);
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
        // The report the manual save wrote at the base name can still be updated by the automatic export.
        let mut ledger2 = Ledger::new();
        let r = save_with(&s, &mut ledger2, false, &[file("v1/a.md", "x")]);
        assert_eq!(outcomes(&r), vec![Outcome::Written]);
        assert_eq!(ledger2.len(), 1);
    }

    #[test]
    fn a_file_without_a_ledger_entry_is_never_updated() {
        let s = Scratch::new().with_root();
        save(&s, &[file("report.md", "from an earlier run or another machine")]).unwrap();
        let r = save_with(&s, &mut Ledger::new(), true, &[file("report.md", "new")]);
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
    }

    #[test]
    fn a_suffixed_copy_is_never_the_one_updated() {
        let s = Scratch::new().with_root();
        let mut ledger = Ledger::new();
        save_with(&s, &mut ledger, true, &[file("report.md", "one")]);
        let dir = s.root().join("my-service").join(T);
        fs::write(dir.join("report.md"), "mine").unwrap();
        save_with(&s, &mut ledger, true, &[file("report.md", "two")]);
        let r = save_with(&s, &mut ledger, true, &[file("report.md", "three")]);
        assert_eq!(outcomes(&r), vec![Outcome::Renamed]);
        assert_eq!(r[0].saved_as, "my-service/thread_ab12/report (2).md");
        assert_eq!(fs::read_to_string(dir.join("report (1).md")).unwrap(), "two");
    }

    #[test]
    fn content_hash_is_stable() {
        assert_eq!(content_hash(b""), "cbf29ce484222325");
        assert_eq!(content_hash(b"a"), "af63dc4c8601ec8c");
    }

    #[test]
    fn config_defaults_for_the_automatic_fields_and_roundtrips_them() {
        let cfg: Config = serde_json::from_str(r#"{"root":"/x"}"#).unwrap();
        assert!(!cfg.auto && cfg.hashes.is_empty() && cfg.marks.is_empty() && cfg.recent.is_empty());
        let cfg: Config = serde_json::from_str(r#"{"root":"/x","auto":true,"autoSinceMs":5}"#).unwrap();
        assert!(cfg.auto && cfg.auto_since_ms == 5);
    }

    #[test]
    fn config_keeps_unknown_keys() {
        let cfg: Config = serde_json::from_str(r#"{"root":"/x","auto":true,"exported":{"t":"h"}}"#).unwrap();
        assert_eq!(cfg.root.as_deref(), Some("/x"));
        let back: serde_json::Value = serde_json::from_str(&serde_json::to_string(&cfg).unwrap()).unwrap();
        assert_eq!(back["auto"], true);
        assert_eq!(back["exported"]["t"], "h");
    }
}
