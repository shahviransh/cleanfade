#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use serde::Deserialize;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::Mutex;

#[derive(Debug, Deserialize)]
struct MonitorConfig {
    sample_rate: u32,
    chunk_seconds: f32,
    duck_percent: f32,
    hold_seconds: f32,
    min_rms: f32,
    model_size: String,
    language: String,
    profanity_file: String,
    input_source: String,
    input_device: String,
    lyrics_mode: bool,
    hf_token: String,
    spotify_token: String,
    playlist_id: String,
    prefetch_playlist_lyrics: bool,
    lyrics_preduck_seconds: f32,
    import_playlist_csv_paths: String,
    prefetch_csv_lyrics: bool,
    prefetch_only: bool,
}

#[derive(Clone, Default)]
struct MonitorState {
    child: Arc<Mutex<Option<CommandChild>>>,
}

fn terminate_monitor_child(child: CommandChild) -> Result<(), String> {
    #[cfg(windows)]
    {
        let pid = child.pid().to_string();
        if let Ok(status) = std::process::Command::new("taskkill")
            .args(["/PID", pid.as_str(), "/T", "/F"])
            .status()
        {
            if status.success() {
                return Ok(());
            }
        }
    }

    child
        .kill()
        .map_err(|e| format!("Failed to stop monitor process: {e}"))
}

async fn stop_monitor_child(child_state: Arc<Mutex<Option<CommandChild>>>) {
    let mut lock = child_state.lock().await;
    if let Some(child) = lock.take() {
        let _ = terminate_monitor_child(child);
    }
}

#[tauri::command]
async fn start_monitor(
    app: AppHandle,
    state: State<'_, MonitorState>,
    config: MonitorConfig,
) -> Result<(), String> {
    let mut lock = state.child.lock().await;
    if lock.is_some() {
        return Err("Monitor is already running.".to_string());
    }

    let mut args = vec![
        "--sample-rate".to_string(),
        config.sample_rate.to_string(),
        "--chunk-seconds".to_string(),
        config.chunk_seconds.to_string(),
        "--duck-percent".to_string(),
        config.duck_percent.to_string(),
        "--hold-seconds".to_string(),
        config.hold_seconds.to_string(),
        "--min-rms".to_string(),
        config.min_rms.to_string(),
        "--model-size".to_string(),
        config.model_size,
        "--language".to_string(),
        config.language,
        "--input-source".to_string(),
        config.input_source,
    ];

    if !config.profanity_file.trim().is_empty() {
        args.push("--profanity-file".to_string());
        args.push(config.profanity_file);
    }

    if !config.input_device.trim().is_empty() {
        args.push("--input-device".to_string());
        args.push(config.input_device);
    }

    if config.lyrics_mode {
        args.push("--lyrics-mode".to_string());
    } else {
        args.push("--no-lyrics-mode".to_string());
    }

    if !config.hf_token.trim().is_empty() {
        args.push("--hf-token".to_string());
        args.push(config.hf_token);
    }

    if !config.spotify_token.trim().is_empty() {
        args.push("--spotify-token".to_string());
        args.push(config.spotify_token);
    }

    if !config.playlist_id.trim().is_empty() {
        args.push("--playlist-id".to_string());
        args.push(config.playlist_id);
    }

    if config.prefetch_playlist_lyrics {
        args.push("--prefetch-playlist-lyrics".to_string());
    }

    let csv_paths: Vec<String> = config
        .import_playlist_csv_paths
        .split(['\n', ';'])
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .collect();

    if config.prefetch_csv_lyrics && !csv_paths.is_empty() {
        args.push("--prefetch-csv-lyrics".to_string());
    }

    for csv_path in csv_paths {
        args.push("--import-playlist-csv".to_string());
        args.push(csv_path);
    }

    if config.prefetch_only {
        args.push("--prefetch-only".to_string());
    }

    args.push("--lyrics-preduck-seconds".to_string());
    args.push(config.lyrics_preduck_seconds.to_string());

    let sidecar = app
        .shell()
        .sidecar("cleanfade-engine")
        .map_err(|e| format!("Failed to setup sidecar: {e}"))?;

    let (mut rx, child) = sidecar
        .args(args)
        .spawn()
        .map_err(|e| format!("Failed to start monitor process: {e}"))?;

    *lock = Some(child);
    drop(lock);

    let app_handle = app.clone();
    let child_state = state.child.clone();

    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(data) => {
                    let line = String::from_utf8_lossy(&data).trim().to_string();
                    if !line.is_empty() {
                        let _ = app_handle.emit("engine-log", line);
                    }
                }
                CommandEvent::Stderr(data) => {
                    let line = String::from_utf8_lossy(&data).trim().to_string();
                    if !line.is_empty() {
                        let _ = app_handle.emit("engine-error", line);
                    }
                }
                CommandEvent::Error(err) => {
                    let _ = app_handle.emit("engine-error", err);
                }
                CommandEvent::Terminated(payload) => {
                    let code = payload.code.unwrap_or_default();
                    let _ = app_handle.emit("engine-stopped", format!("Monitor exited with code {code}"));

                    let mut lock = child_state.lock().await;
                    *lock = None;
                    break;
                }
                _ => {}
            }
        }
    });

    app.emit("engine-log", "Monitor started.")
        .map_err(|e| format!("Failed to emit start event: {e}"))?;

    Ok(())
}

#[tauri::command]
async fn stop_monitor(state: State<'_, MonitorState>) -> Result<(), String> {
    let mut lock = state.child.lock().await;
    let child = lock
        .take()
        .ok_or_else(|| "Monitor is not running.".to_string())?;

    terminate_monitor_child(child)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(MonitorState::default())
        .on_window_event(|window, event| {
            if !matches!(event, WindowEvent::CloseRequested { .. }) {
                return;
            }

            let child_state = window.state::<MonitorState>().child.clone();
            tauri::async_runtime::spawn(async move {
                stop_monitor_child(child_state).await;
            });
        })
        .invoke_handler(tauri::generate_handler![start_monitor, stop_monitor])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
