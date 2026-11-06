//! Window enumeration + focus via CGWindowList + NSRunningApplication.
//!
//! `list_windows()` returns every on-screen window with its CGWindowID,
//! owning pid, bundle id, title, bounds, and layer. The agent uses this
//! to pick a target ("the Linear window") or to figure out what app is
//! frontmost so it can route follow-up AX queries to the right pid.
//!
//! `focus_window()` activates the owning application via
//! NSRunningApplication, which brings its key window to the front, then
//! finds the window among the app's AX windows and raises it with AXRaise
//! and AXMain, so the window asked for is the one in front. When the AX
//! window cannot be told apart from the app's others, only the app is
//! activated, as before.

use crate::ax::{frame_matches, AX_MESSAGING_TIMEOUT_SECS};
use accessibility::{AXUIElement, AXUIElementAttributes};
use accessibility_sys::{kAXErrorSuccess, AXError, AXUIElementRef, AXUIElementSetMessagingTimeout};
use core_foundation::array::{CFArray, CFArrayRef};
use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::window::{
    kCGNullWindowID, kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
};
use std::ffi::{c_char, c_void};
use std::sync::OnceLock;
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
    /// The app's display name as the window server reports it (kCGWindowOwnerName).
    pub owner: String,
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

/// Bring a specific CGWindow to the front. macOS has no call that raises a
/// CGWindow, so this activates the owning NSRunningApplication, which brings
/// the app's key window forward, and then raises the window itself through
/// its AX window (`raise_ax_window`). When that window cannot be found, the
/// activation alone stands, as it did before.
pub fn focus_window(window_id: u32) -> Result<(), WindowError> {
    let windows = list_windows()?;
    let Some(target) = windows.into_iter().find(|w| w.id == window_id) else {
        return Err(WindowError::NoApp(-1));
    };
    activate_pid(target.pid)?;
    raise_ax_window(&target);
    Ok(())
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

// ─── Raising one window ─────────────────────────────────────────────────

/// What `pick_ax_window` knows of one of an app's AX windows.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct AxWindowFacts {
    /// Its CGWindowID, from `_AXUIElementGetWindow`. None when that call is
    /// missing or fails for this window.
    pub number: Option<u32>,
    /// AXFrame: global top-left points, the space CG window bounds use.
    pub frame: Option<(f64, f64, f64, f64)>,
    pub title: Option<String>,
}

/// Which of an app's AX windows is the CG window `target`. The window whose
/// number is the target's id, when there is one. Otherwise, among the windows
/// whose number could not be read, the single one whose frame is the target's
/// bounds (within 2 points) and whose title, when both titles are known, is
/// the target's title. A window whose number is known and different is never
/// picked. None when no window fits, or when more than one does, so a window
/// that cannot be told apart from its siblings is left alone.
pub(crate) fn pick_ax_window(windows: &[AxWindowFacts], target: &WindowInfo) -> Option<usize> {
    if let Some(i) = windows.iter().position(|w| w.number == Some(target.id)) {
        return Some(i);
    }
    let bounds = (target.x, target.y, target.w, target.h);
    let mut fits = windows.iter().enumerate().filter(|(_, w)| {
        w.number.is_none()
            && frame_matches(w.frame, bounds)
            && (target.title.is_empty()
                || w.title.as_deref().is_none_or(|t| t == target.title))
    });
    let (first, _) = fits.next()?;
    if fits.next().is_some() {
        return None;
    }
    Some(first)
}

type GetWindowFn = unsafe extern "C" fn(AXUIElementRef, *mut u32) -> AXError;

extern "C" {
    fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
}

/// `_AXUIElementGetWindow`, the private HIServices call that gives an AX
/// window's CGWindowID (window managers such as yabai and AltTab use it).
/// Looked up at run time, not linked, so a macOS without it still loads this
/// module; the frame and title match is used then.
fn ax_get_window() -> Option<GetWindowFn> {
    static FOUND: OnceLock<Option<GetWindowFn>> = OnceLock::new();
    *FOUND.get_or_init(|| {
        // RTLD_DEFAULT on macOS; ApplicationServices is linked by accessibility-sys.
        let rtld_default = -2isize as *mut c_void;
        let ptr = unsafe { dlsym(rtld_default, c"_AXUIElementGetWindow".as_ptr()) };
        if ptr.is_null() {
            None
        } else {
            Some(unsafe { std::mem::transmute::<*mut c_void, GetWindowFn>(ptr) })
        }
    })
}

fn ax_window_facts(window: &AXUIElement, get_window: Option<GetWindowFn>) -> AxWindowFacts {
    let number = get_window.and_then(|f| {
        let mut id: u32 = 0;
        let err = unsafe { f(window.as_concrete_TypeRef(), &mut id) };
        (err == kAXErrorSuccess && id != 0).then_some(id)
    });
    AxWindowFacts {
        number,
        frame: crate::ax::frame_of(window),
        title: window.title().ok().map(|t| t.to_string()),
    }
}

/// Raise `target` above its app's other windows and make it the app's main
/// window, through the app's AX window for it. Best effort: does nothing when
/// the app does not answer or `pick_ax_window` finds no single match.
fn raise_ax_window(target: &WindowInfo) {
    let app = AXUIElement::application(target.pid);
    unsafe {
        AXUIElementSetMessagingTimeout(app.as_concrete_TypeRef(), AX_MESSAGING_TIMEOUT_SECS);
    }
    let Ok(list) = app.windows() else {
        return;
    };
    let windows: Vec<AXUIElement> = list.iter().map(|w| w.clone()).collect();
    for w in &windows {
        unsafe {
            AXUIElementSetMessagingTimeout(w.as_concrete_TypeRef(), AX_MESSAGING_TIMEOUT_SECS);
        }
    }
    let get_window = ax_get_window();
    let facts: Vec<AxWindowFacts> = windows.iter().map(|w| ax_window_facts(w, get_window)).collect();
    let Some(i) = pick_ax_window(&facts, target) else {
        return;
    };
    let window = &windows[i];
    let _ = window.perform_action(&CFString::from_static_string("AXRaise"));
    let _ = window.set_main(CFBoolean::true_value());
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
    let bundle = resolve_bundle_for_pid(pid).unwrap_or_else(|| name.clone());
    let is_frontmost = frontmost_pid == Some(pid) && layer == 0;
    Some(WindowInfo {
        id,
        pid,
        bundle,
        owner: name,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn target(id: u32, title: &str) -> WindowInfo {
        WindowInfo {
            id,
            pid: 42,
            bundle: "com.example.app".into(),
            owner: "Example".into(),
            title: title.into(),
            x: 100.0,
            y: 50.0,
            w: 800.0,
            h: 600.0,
            is_frontmost: false,
            layer: 0,
        }
    }

    fn ax(number: Option<u32>, frame: (f64, f64, f64, f64), title: &str) -> AxWindowFacts {
        AxWindowFacts { number, frame: Some(frame), title: Some(title.into()) }
    }

    const SAME: (f64, f64, f64, f64) = (100.0, 50.0, 800.0, 600.0);

    #[test]
    fn the_window_number_picks_the_window_even_when_frames_are_the_same() {
        // Two windows of one app stacked at the same place: only the number
        // tells them apart.
        let windows = [ax(Some(7), SAME, "Inbox"), ax(Some(9), SAME, "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), Some(1));
    }

    #[test]
    fn a_known_number_that_differs_is_never_picked_by_frame() {
        // The target is not among the AX windows (another Space, a panel).
        let windows = [ax(Some(7), SAME, "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), None);
    }

    #[test]
    fn without_numbers_the_one_window_with_the_bounds_is_picked() {
        let windows = [
            ax(None, (0.0, 0.0, 400.0, 300.0), "Other"),
            ax(None, (101.0, 49.0, 801.5, 600.0), "Inbox"),
        ];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), Some(1));
    }

    #[test]
    fn without_numbers_the_title_separates_windows_with_the_same_bounds() {
        let windows = [ax(None, SAME, "Drafts"), ax(None, SAME, "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), Some(1));
    }

    #[test]
    fn without_numbers_windows_that_cannot_be_told_apart_are_left_alone() {
        // No CG title (no Screen Recording), two windows at the same place.
        let windows = [ax(None, SAME, "Drafts"), ax(None, SAME, "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "")), None);
        let windows = [ax(None, SAME, "Inbox"), ax(None, SAME, "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), None);
    }

    #[test]
    fn without_numbers_a_window_elsewhere_or_with_another_title_is_not_picked() {
        let windows = [ax(None, (100.0, 50.0, 820.0, 600.0), "Inbox")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), None);
        let windows = [ax(None, SAME, "Drafts")];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), None);
        let windows = [AxWindowFacts { number: None, frame: None, title: None }];
        assert_eq!(pick_ax_window(&windows, &target(9, "")), None);
        assert_eq!(pick_ax_window(&[], &target(9, "")), None);
    }

    #[test]
    fn without_numbers_a_window_with_no_ax_title_matches_by_bounds() {
        let windows = [AxWindowFacts { number: None, frame: Some(SAME), title: None }];
        assert_eq!(pick_ax_window(&windows, &target(9, "Inbox")), Some(0));
    }

    #[test]
    fn the_window_number_call_is_found() {
        // Only looks the symbol up; nothing is called.
        assert!(ax_get_window().is_some());
    }
}
