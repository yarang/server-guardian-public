//! AGENT_TOKEN storage in the OS credential store, one entry per agent origin.
//!
//! The dashboard keeps the token in memory and persists it only through these commands, so it
//! is never written to the WebView's localStorage on disk. Keying by origin means a token saved
//! for one agent is never sent to another one the user points the app at.

use keyring::{Entry, Error};

const SERVICE: &str = "com.serverguardian.desktop";
const MAX_TOKEN_LEN: usize = 4096;

/// Normalizes the agent URL to `scheme://host[:port]` and rejects anything that is not http(s).
fn account(agent: &str) -> Result<String, String> {
    let agent = agent.trim().trim_end_matches('/');
    let rest = agent
        .strip_prefix("http://")
        .map(|r| ("http", r))
        .or_else(|| agent.strip_prefix("https://").map(|r| ("https", r)));
    let (scheme, rest) = rest.ok_or_else(|| "agent URL must be http(s)".to_string())?;
    let host = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if host.is_empty() || host.len() > 255 || host.contains('@') || host.chars().any(char::is_whitespace) {
        return Err("invalid agent URL".to_string());
    }
    Ok(format!("{scheme}://{}", host.to_ascii_lowercase()))
}

fn entry(agent: &str) -> Result<Entry, String> {
    Entry::new(SERVICE, &account(agent)?).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn token_get(agent: String) -> Result<Option<String>, String> {
    match entry(&agent)?.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
pub fn token_set(agent: String, token: String) -> Result<(), String> {
    if token.is_empty() || token.len() > MAX_TOKEN_LEN {
        return Err("invalid token".to_string());
    }
    entry(&agent)?.set_password(&token).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn token_delete(agent: String) -> Result<(), String> {
    match entry(&agent)?.delete_credential() {
        Ok(()) | Err(Error::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::account;

    #[test]
    fn account_is_the_normalized_origin() {
        assert_eq!(account("http://100.64.0.7:9870/").unwrap(), "http://100.64.0.7:9870");
        assert_eq!(account(" https://Hub.example:9870/api?x=1 ").unwrap(), "https://hub.example:9870");
    }

    #[test]
    fn account_rejects_other_schemes_and_userinfo() {
        for bad in ["", "ftp://hub", "hub:9870", "http://", "http://user@hub:9870", "http://a b"] {
            assert!(account(bad).is_err(), "{bad:?} should be rejected");
        }
    }
}
