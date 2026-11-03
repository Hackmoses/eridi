//! Eridi's desktop app: a native window and a tray (menu-bar) icon around the engine.
//!
//! The engine is the packaged Python app (`eridi-engine`), started as a sidecar with
//! `serve --sidecar`. It prints one JSON object per line on stdout; the first is
//! `{"event": "ready", "url": ...}`, a one-time sign-in link to the local web app, which
//! the window then shows. The app writes commands to the engine's stdin (`link`, `scan`,
//! `quit`). If this app goes away, the engine sees its stdin close and stops.
//!
//! Closing the window keeps Eridi in the tray, so background scanning goes on; "Quit Eridi"
//! in the tray menu stops both.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, Url, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

const LOCAL_PREFIX: &str = "http://127.0.0.1:";

#[derive(Default)]
struct Engine {
    child: Mutex<Option<CommandChild>>,
    stopping: Mutex<bool>,
}

#[derive(Deserialize)]
struct EngineMessage {
    event: String,
    url: Option<String>,
}

fn main() {
    tauri::Builder::default()
        // A second launch just brings the running Eridi forward.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_window(app)
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Engine::default())
        .setup(|app| {
            start_engine(app.handle())?;
            build_tray(app.handle())?;
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move { check_for_updates(handle, false).await });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Keep running in the tray, so files are still watched and scanned.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("Eridi couldn't start")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_engine(app);
            }
        });
}

fn start_engine(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let (mut events, child) = app
        .shell()
        .sidecar("eridi-engine")?
        .args(["serve", "--sidecar"])
        .spawn()?;
    *app.state::<Engine>().child.lock().unwrap() = Some(child);
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    for line in String::from_utf8_lossy(&bytes).lines() {
                        on_engine_message(&handle, line.trim());
                    }
                }
                CommandEvent::Stderr(bytes) => eprint!("{}", String::from_utf8_lossy(&bytes)),
                CommandEvent::Terminated(status) => {
                    if !*handle.state::<Engine>().stopping.lock().unwrap() {
                        let code = status.code.map(|c| c.to_string()).unwrap_or_default();
                        handle
                            .dialog()
                            .message(format!(
                                "Eridi's engine stopped unexpectedly (code {code}). Quit and \
                                 open Eridi again. If it keeps happening, run `eridi doctor` \
                                 in a terminal."
                            ))
                            .title("Eridi")
                            .kind(MessageDialogKind::Error)
                            .show(|_| {});
                    }
                    break;
                }
                _ => {}
            }
        }
    });
    Ok(())
}

fn on_engine_message(app: &AppHandle, line: &str) {
    let Ok(message) = serde_json::from_str::<EngineMessage>(line) else {
        return;
    };
    if message.event != "ready" && message.event != "link" {
        return;
    }
    // Only ever navigate to the engine on this computer.
    let Some(url) = message.url.filter(|u| u.starts_with(LOCAL_PREFIX)) else {
        return;
    };
    if let (Some(window), Ok(url)) = (app.get_webview_window("main"), Url::parse(&url)) {
        let _ = window.navigate(url);
    }
}

fn send(app: &AppHandle, command: &str) {
    if let Some(child) = app.state::<Engine>().child.lock().unwrap().as_mut() {
        let _ = child.write(format!("{command}\n").as_bytes());
    }
}

fn stop_engine(app: &AppHandle) {
    *app.state::<Engine>().stopping.lock().unwrap() = true;
    if let Some(mut child) = app.state::<Engine>().child.lock().unwrap().take() {
        // The engine stops on "quit", and also as soon as its stdin closes with this app.
        let _ = child.write(b"quit\n");
    }
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Eridi", true, None::<&str>)?;
    let scan = MenuItem::with_id(app, "scan", "Scan now", true, None::<&str>)?;
    let update = MenuItem::with_id(app, "update", "Check for updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Eridi", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &scan, &update, &separator, &quit])?;
    let mut tray = TrayIconBuilder::with_id("eridi")
        .tooltip("Eridi")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_window(app),
            "scan" => send(app, "scan"),
            "update" => {
                let handle = app.clone();
                tauri::async_runtime::spawn(async move { check_for_updates(handle, true).await });
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

/// Release builds carry the updater's public key; local builds don't, and never check.
fn updates_configured(app: &AppHandle) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|u| u.get("pubkey"))
        .and_then(|k| k.as_str())
        .is_some_and(|k| !k.is_empty())
}

fn tell(app: &AppHandle, text: String, kind: MessageDialogKind) {
    app.dialog()
        .message(text)
        .title("Eridi")
        .kind(kind)
        .show(|_| {});
}

async fn check_for_updates(app: AppHandle, asked: bool) {
    if !updates_configured(&app) {
        if asked {
            tell(
                &app,
                "This copy of Eridi was built without updates.".into(),
                MessageDialogKind::Info,
            );
        }
        return;
    }
    let update = match app.updater() {
        Ok(updater) => updater.check().await,
        Err(err) => Err(err),
    };
    let update = match update {
        Ok(Some(update)) => update,
        Ok(None) => {
            if asked {
                tell(&app, "Eridi is up to date.".into(), MessageDialogKind::Info);
            }
            return;
        }
        Err(err) => {
            if asked {
                tell(
                    &app,
                    format!("Couldn't check for updates: {err}"),
                    MessageDialogKind::Warning,
                );
            }
            return;
        }
    };
    let question = app.clone();
    let version = update.version.clone();
    let install = tauri::async_runtime::spawn_blocking(move || {
        question
            .dialog()
            .message(format!(
                "Eridi {version} is available. Install it and restart?"
            ))
            .title("Update Eridi")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Install and restart".into(),
                "Later".into(),
            ))
            .blocking_show()
    })
    .await
    .unwrap_or(false);
    if !install {
        return;
    }
    stop_engine(&app); // the installer replaces the engine too
    match update.download_and_install(|_, _| {}, || {}).await {
        Ok(()) => app.restart(),
        Err(err) => tell(
            &app,
            format!("The update didn't install: {err}. Download it from the Eridi releases page."),
            MessageDialogKind::Error,
        ),
    }
}
