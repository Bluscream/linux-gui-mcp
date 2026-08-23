//! Running the programs this crate leans on.
//!
//! Every module here works by calling something else - ydotool, kdotool,
//! busctl, wl-copy - so the awkward parts of doing that live in one place: a
//! missing binary, a non-zero exit with the reason on stderr, and the session
//! environment without which none of them can find the desktop.

use anyhow::{bail, Context, Result};
use std::collections::HashMap;
use std::sync::OnceLock;
use tokio::process::Command;

/// The environment a program needs to find the desktop session.
///
/// Filled in rather than inherited, because this server is started by an MCP
/// client and may inherit almost nothing. Without these, the tools do not fail
/// helpfully - kdotool tries to autolaunch its own D-Bus daemon and dies
/// complaining about X11.
pub fn session_env() -> &'static HashMap<String, String> {
    static ENV: OnceLock<HashMap<String, String>> = OnceLock::new();
    ENV.get_or_init(|| {
        let mut env: HashMap<String, String> = std::env::vars().collect();
        let uid = unsafe { getuid() };
        let runtime = env
            .get("XDG_RUNTIME_DIR")
            .cloned()
            .unwrap_or_else(|| format!("/run/user/{uid}"));
        env.insert("XDG_RUNTIME_DIR".into(), runtime.clone());
        env.entry("DBUS_SESSION_BUS_ADDRESS".into())
            .or_insert_with(|| format!("unix:path={runtime}/bus"));
        env.entry("WAYLAND_DISPLAY".into()).or_insert("wayland-0".into());
        env.entry("DISPLAY".into()).or_insert(":0".into());
        env
    })
}

unsafe extern "C" {
    fn getuid() -> u32;
}

/// Run a program and return its output, or fail with the reason.
pub async fn run(argv: &[&str]) -> Result<String> {
    let (program, args) = argv.split_first().context("no command given")?;
    let output = Command::new(program)
        .args(args)
        .envs(session_env())
        .output()
        .await
        .with_context(|| format!("{program} could not be started; is it installed?"))?;

    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        let detail = if detail.trim().is_empty() {
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        } else {
            detail.trim().to_string()
        };
        bail!("{program} failed: {}", if detail.is_empty() { "no output".into() } else { detail });
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Whether a program is on PATH.
pub fn have(program: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| dir.join(program).is_file())
        })
        .unwrap_or(false)
}

/// Wait, so an interface has time to redraw before it is looked at.
///
/// Input arrives the instant it is sent, but nothing has redrawn yet. A
/// screenshot taken immediately shows the state before the action, which reads
/// exactly like the action having failed.
pub async fn settle(milliseconds: u64) {
    if milliseconds > 0 {
        tokio::time::sleep(std::time::Duration::from_millis(milliseconds)).await;
    }
}
