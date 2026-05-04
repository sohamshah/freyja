//! Window enumeration + focus via CGWindowList + NSRunningApplication.
//!
//! `list_windows()` returns every on-screen window with its CGWindowID,
//! owning pid, bundle id, title, bounds, and layer. The agent uses this
//! to pick a target ("the Linear window") or to figure out what app is
//! frontmost so it can route follow-up AX queries to the right pid.
//!
//! `focus_window()` is best-effort: macOS doesn't let us raise a
//! specific CGWindow directly, but we can activate the owning
//! application via NSRunningApplication, which brings its key window to
//! the front. That's the behavior Vy's `defocus` module provides too.

use core_foundation::array::{CFArray, CFArrayRef};
use core_foundation::base::{CFType, TCFType};
use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::window::{
    kCGNullWindowID, kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum WindowError {
    #[error("CGWindowListCopyWindowInfo returned null")]
    ListFailed,
    #[error("no running application for pid {0}")]
    NoApp(i32),
    #[error("activate failed")]
    ActivateFailed,
}

#[derive(Debug, Clone)]
pub struct WindowInfo {
    pub id: u32,
    pub pid: i32,
    pub bundle: String,
    pub title: String,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
    pub is_frontmost: bool,
    pub layer: i64,
}

// External CG APIs not re-exported by the core-graphics crate at its
// current version.
#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGWindowListCopyWindowInfo(
        option: u32,
        relativeToWindow: u32,
    ) -> CFArrayRef;
}

/// Enumerate all on-screen windows, top to bottom.
pub fn list_windows() -> Result<Vec<WindowInfo>, WindowError> {
    let options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements;
    let array_ref: CFArrayRef =
        unsafe { CGWindowListCopyWindowInfo(options, kCGNullWindowID) };
    if array_ref.is_null() {
        return Err(WindowError::ListFailed);
    }
    let array: CFArray<CFType> = unsafe { CFArray::wrap_under_create_rule(array_ref) };
    let frontmost_pid = frontmost_app_pid();

    let mut out = Vec::with_capacity(array.len() as usize);
    for i in 0..array.len() {
        let Some(item) = array.get(i) else { continue };
        let dict_ref = item.as_CFTypeRef() as CFDictionaryRef;
        if dict_ref.is_null() {
            continue;
        }
        let dict: CFDictionary<CFString, CFType> =
            unsafe { CFDictionary::wrap_under_get_rule(dict_ref) };
        let Some(info) = decode_window_dict(&dict, frontmost_pid) else {
            continue;
        };
        out.push(info);
    }
    Ok(out)
}

/// Get the frontmost regular window (layer == 0, matching a real app
/// window, not a menu or shadow overlay).
pub fn get_frontmost_window() -> Result<Option<WindowInfo>, WindowError> {
    let windows = list_windows()?;
    // CGWindowListCopyWindowInfo returns windows front-to-back already;
    // we just pick the first normal-layer one owned by the frontmost app.
    let frontmost_pid = frontmost_app_pid();
    for w in windows {
        if w.layer == 0 && Some(w.pid) == frontmost_pid {
            return Ok(Some(w));
        }
    }
    Ok(None)
}

/// Focus the application that owns a specific CGWindow. macOS doesn't
/// expose "raise this CGWindow" directly, so we activate the owning
/// NSRunningApplication.
pub fn focus_window(window_id: u32) -> Result<(), WindowError> {
    let windows = list_windows()?;
    let Some(target) = windows.into_iter().find(|w| w.id == window_id) else {
        return Err(WindowError::NoApp(-1));
    };
    activate_pid(target.pid)
}

/// Focus an app by bundle id, e.g. "com.apple.finder".
pub fn focus_app(bundle_id: &str) -> Result<(), WindowError> {
    // Enumerate running apps and activate the first match.
    let windows = list_windows()?;
    if let Some(w) = windows
        .into_iter()
        .find(|w| w.bundle.eq_ignore_ascii_case(bundle_id))
    {
        return activate_pid(w.pid);
    }
    Err(WindowError::NoApp(-1))
}

// ─── Helpers ────────────────────────────────────────────────────────────

fn decode_window_dict(
    dict: &CFDictionary<CFString, CFType>,
    frontmost_pid: Option<i32>,
) -> Option<WindowInfo> {
    let id = get_number(dict, "kCGWindowNumber")? as u32;
    let pid = get_number(dict, "kCGWindowOwnerPID")? as i32;
    let layer = get_number(dict, "kCGWindowLayer").unwrap_or(0);
    let name = get_string(dict, "kCGWindowOwnerName").unwrap_or_default();
    let title = get_string(dict, "kCGWindowName").unwrap_or_default();
    let (x, y, w, h) = get_bounds(dict).unwrap_or((0.0, 0.0, 0.0, 0.0));

    // We use the owner name as a proxy for the bundle id — the real
    // bundle is easier to resolve from NSRunningApplication at focus
    // time, and name-based matching is what users type anyway. Skip
    // zero-size windows; they're invisible artifacts.
    if w <= 0.0 || h <= 0.0 {
        return None;
    }
    let bundle = resolve_bundle_for_pid(pid).unwrap_or(name);
    let is_frontmost = frontmost_pid == Some(pid) && layer == 0;
    Some(WindowInfo {
        id,
        pid,
        bundle,
        title,
        x,
        y,
        w,
        h,
        is_frontmost,
        layer,
    })
}

fn get_number(dict: &CFDictionary<CFString, CFType>, key: &str) -> Option<i64> {
    let cf_key = CFString::new(key);
    let val = dict.find(&cf_key)?;
    let num_ptr = val.as_CFTypeRef() as core_foundation::number::CFNumberRef;
    let num: CFNumber = unsafe { CFNumber::wrap_under_get_rule(num_ptr) };
    num.to_i64()
}

fn get_string(dict: &CFDictionary<CFString, CFType>, key: &str) -> Option<String> {
    let cf_key = CFString::new(key);
    let val = dict.find(&cf_key)?;
    let str_ptr = val.as_CFTypeRef() as core_foundation::string::CFStringRef;
    let s: CFString = unsafe { CFString::wrap_under_get_rule(str_ptr) };
    Some(s.to_string())
}

fn get_bounds(dict: &CFDictionary<CFString, CFType>) -> Option<(f64, f64, f64, f64)> {
    let cf_key = CFString::new("kCGWindowBounds");
    let val = dict.find(&cf_key)?;
    let inner_ref = val.as_CFTypeRef() as CFDictionaryRef;
    if inner_ref.is_null() {
        return None;
    }
    let inner: CFDictionary<CFString, CFType> =
        unsafe { CFDictionary::wrap_under_get_rule(inner_ref) };
    let x = get_number_f64(&inner, "X")?;
    let y = get_number_f64(&inner, "Y")?;
    let w = get_number_f64(&inner, "Width")?;
    let h = get_number_f64(&inner, "Height")?;
    Some((x, y, w, h))
}

fn get_number_f64(dict: &CFDictionary<CFString, CFType>, key: &str) -> Option<f64> {
    let cf_key = CFString::new(key);
    let val = dict.find(&cf_key)?;
    let num_ptr = val.as_CFTypeRef() as core_foundation::number::CFNumberRef;
    let num: CFNumber = unsafe { CFNumber::wrap_under_get_rule(num_ptr) };
    num.to_f64()
}

/// Resolve the bundle id for a pid via NSRunningApplication.
fn resolve_bundle_for_pid(pid: i32) -> Option<String> {
    use objc2_app_kit::NSRunningApplication;

    unsafe {
        let app = NSRunningApplication::runningApplicationWithProcessIdentifier(pid)?;
        let bundle = app.bundleIdentifier()?;
        Some(bundle.to_string())
    }
}

/// Pid of the frontmost app according to NSWorkspace.
fn frontmost_app_pid() -> Option<i32> {
    use objc2_app_kit::NSWorkspace;
    unsafe {
        let ws = NSWorkspace::sharedWorkspace();
        let app = ws.frontmostApplication()?;
        Some(app.processIdentifier() as i32)
    }
}

/// Activate (bring to front) the NSRunningApplication for a pid.
#[allow(deprecated)] // NSApplicationActivateIgnoringOtherApps is deprecated in macOS 14
fn activate_pid(pid: i32) -> Result<(), WindowError> {
    use objc2_app_kit::{NSApplicationActivationOptions, NSRunningApplication};
    unsafe {
        let Some(app) = NSRunningApplication::runningApplicationWithProcessIdentifier(pid) else {
            return Err(WindowError::NoApp(pid));
        };
        let options = NSApplicationActivationOptions::NSApplicationActivateAllWindows
            | NSApplicationActivationOptions::NSApplicationActivateIgnoringOtherApps;
        let ok: bool = app.activateWithOptions(options);
        if ok {
            Ok(())
        } else {
            Err(WindowError::ActivateFailed)
        }
    }
}
