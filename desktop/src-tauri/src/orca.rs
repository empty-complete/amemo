//! Orca owns installation and consent. The wizard only passes the bundled folder and the
//! fingerprint the user has approved.

use serde::Serialize;
use serde_json::Value;
use std::{
    env,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
use tauri::{path::BaseDirectory, AppHandle, Manager};

const PLUGIN: &str = "plugins/amemo-orca";
const PLUGIN_KEY: &str = "local.amemo";

pub fn cli() -> Option<PathBuf> {
    let search_path = env::var_os("PATH").unwrap_or_default();
    [
        ("LOCALAPPDATA", "Programs/orca/resources/bin/orca.exe"),
        ("ProgramFiles", "Orca/resources/bin/orca.exe"),
    ]
    .into_iter()
    .filter_map(|(key, suffix)| env::var_os(key).map(|root| PathBuf::from(root).join(suffix)))
    .chain(env::split_paths(&search_path).map(|dir| dir.join("orca.exe")))
    .find(|path| path.is_absolute() && path.is_file())
}

#[derive(Serialize)]
#[serde(tag = "status", rename_all = "camelCase")]
pub enum InstallResult {
    Installed,
    Approval { fingerprint: String, access: String },
}

fn install(cli: &Path, bundle: &Path, approval: Option<String>) -> Result<InstallResult, String> {
    if !bundle.join("orca-plugin.json").is_file() {
        return Err("The amemo plugin bundle is missing.".into());
    }
    let mut command = Command::new(cli);
    crate::setup::no_window(&mut command);
    command.args(["plugin", "install"]).arg(bundle)
        .args(["--environment", "local", "--json"]).stdin(Stdio::null());
    if let Some(fingerprint) = approval {
        command.args(["--approve", &fingerprint]);
    }
    let result = command.output().map_err(|error| format!("Orca CLI: {error}"))?;
    let reply: Value = serde_json::from_slice(&result.stdout).map_err(|_| {
        let detail = String::from_utf8_lossy(&result.stderr);
        format!("Orca CLI ({}): {}", result.status, detail.trim())
    })?;
    if result.status.success() && reply["ok"] == true && reply["result"]["pluginKey"] == PLUGIN_KEY {
        return Ok(InstallResult::Installed);
    }
    let error = &reply["error"];
    let data = &error["data"];
    if matches!(error["code"].as_str(), Some("approval_required" | "approval_stale"))
        && data["pluginKey"] == PLUGIN_KEY
        && data["grants"]["execution"] == "trusted-worker"
        && data["grants"]["capabilities"].as_array().is_some_and(Vec::is_empty)
    {
        if let (Some(fingerprint), Some(access)) = (
            data["approvalFingerprint"].as_str(),
            data["grants"]["executionDescription"].as_str(),
        ) {
            return Ok(InstallResult::Approval { fingerprint: fingerprint.into(), access: access.into() });
        }
    }
    Err(error["message"].as_str().unwrap_or("Orca did not confirm installation.").into())
}

#[tauri::command]
pub async fn setup_orca(app: AppHandle, approval: Option<String>) -> Result<InstallResult, String> {
    let cli = cli().ok_or("Orca CLI was not found. Install or update Orca IDE and open it first.")?;
    let bundle = app.path().resolve(PLUGIN, BaseDirectory::Resource).map_err(|error| error.to_string())?;
    tauri::async_runtime::spawn_blocking(move || install(&cli, &bundle, approval))
        .await.map_err(|error| error.to_string())?
}
