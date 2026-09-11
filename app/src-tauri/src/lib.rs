mod collector;
mod credentials;
mod placement;
mod sidecar;
mod updater;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager, RunEvent, WindowEvent,
};
use tauri_plugin_window_state::StateFlags;

/// One recovery path for both the main app and the tray. A hidden or
/// minimized companion is always brought forward without touching its clock.
#[tauri::command]
fn show_companion(app: tauri::AppHandle) -> Result<(), String> {
    let deer = app
        .get_webview_window("deer")
        .ok_or("Companion window is unavailable")?;
    deer.unminimize().map_err(|e| e.to_string())?;
    deer.show().map_err(|e| e.to_string())?;
    deer.set_focus().map_err(|e| e.to_string())
}

/// The companion (and the tray) raising chordial itself: the stuck page
/// lives in the main window (docs/STUCK_MODE_DESIGN.md section 1).
#[tauri::command]
fn show_main(app: tauri::AppHandle) -> Result<(), String> {
    let main = app
        .get_webview_window("main")
        .ok_or("Main window is unavailable")?;
    main.unminimize().map_err(|e| e.to_string())?;
    main.show().map_err(|e| e.to_string())?;
    main.set_focus().map_err(|e| e.to_string())
}

/// The tray's press: raise the main window and tell it. The frontend
/// listens for `chordial:stuck` and opens the page.
#[derive(Clone, serde::Serialize)]
struct StuckPress {
    surface: &'static str,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        // windows remember where the person put them (size + position,
        // restored on creation, saved on exit). visibility is deliberately
        // NOT remembered: a deer tucked away from the tray at quit still
        // greets the next launch - hiding is for the day, not forever.
        .plugin(
            tauri_plugin_window_state::Builder::new()
                .with_state_flags(StateFlags::all() & !StateFlags::VISIBLE)
                .build(),
        )
        .manage(sidecar::SidecarState::new())
        // neither window is destroyed while the app runs: a platform close
        // (cmd-w, the window menu) HIDES it, so the tray can always bring
        // it back. the companion: her own close button does the same and
        // the sidecar's clock keeps counting underneath (sol's #84 round -
        // a destroyed window could not be re-shown). the main window: the
        // companion's and the tray's "i'm stuck" raise it with show_main,
        // which can only show a window that still exists (sol's #91
        // round). quitting is the tray's or cmd-q's, never a close.
        .on_window_event(|window, event| {
            if window.label() == "deer" || window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            show_companion,
            show_main,
            credentials::credential_get,
            credentials::credential_set,
            credentials::credential_clear,
        ])
        .setup(|app| {
            // the collectors: frontmost bundle id + idle, 2s poll, loopback
            // to the sidecar. sanitized inside collector.rs - the boundary.
            collector::spawn();

            // the box (7b): release builds spawn and supervise the bundled
            // sidecar; debug builds leave it to the dev loop's terminal
            sidecar::spawn(app.handle());

            // first launch only: deer to the bottom-right corner, main
            // window beside her - never the deer on top of the link field.
            // afterwards the window-state plugin remembers their places.
            placement::apply_first_launch(app.handle());

            // a quiet look at the update feed once the windows are up
            updater::check_on_launch(app.handle());

            // the tray: chordial lives in the corner of the day, so the
            // deer can be tucked away and called back without the dock
            let stuck = MenuItem::with_id(
                app, "stuck", "I’m stuck", true, None::<&str>,
            )?;
            let show = MenuItem::with_id(
                app, "show-deer", "Show companion", true,
                None::<&str>,
            )?;
            let hide = MenuItem::with_id(
                app, "hide-deer", "Hide companion", true, None::<&str>,
            )?;
            let check = MenuItem::with_id(
                app, "check-updates", "check for updates", true,
                None::<&str>,
            )?;
            let quit = MenuItem::with_id(
                app, "quit", "quit chordial", true, None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&stuck, &show, &hide, &check, &quit])?;
            let mut tray = TrayIconBuilder::new().menu(&menu).on_menu_event(
                |app, event| match event.id.as_ref() {
                    "stuck" => {
                        let _ = show_main(app.clone());
                        let _ = app.emit("chordial:stuck", StuckPress { surface: "tray" });
                    }
                    "show-deer" => {
                        let _ = show_companion(app.clone());
                    }
                    "hide-deer" => {
                        if let Some(deer) = app.get_webview_window("deer") {
                            let _ = deer.hide();
                        }
                    }
                    "check-updates" => updater::check_interactive(app),
                    "quit" => app.exit(0),
                    _ => {}
                },
            );
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        // the shell owns the sidecar's whole life: whatever path led to
        // exit (tray quit, cmd-q, an update restart), the child dies here
        if let RunEvent::Exit = event {
            sidecar::shutdown(app_handle);
        }
    });
}
