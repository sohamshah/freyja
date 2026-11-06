//! Native macOS bindings for agent-harness computer-use tools.
//!
//! This crate is a thin Rust "subplatform" loaded into the Python bridge
//! as a pyo3 extension module. It exposes a small set of functions the
//! Python tool layer calls to capture the screen, inject input, enumerate
//! windows, and read the accessibility tree. Nothing in here is
//! particularly smart — the intelligence lives in the Python tool
//! wrappers and the LLM driving them.
//!
//! Exposed functions (see each module for details):
//!
//!   capture_screen(display_id?)     -> bytes (PNG)
//!   capture_window(window_id)       -> bytes (PNG)
//!   click(x, y, button, double, modifiers)
//!   move_mouse(x, y)
//!   type_text(text)
//!   press_key(key, modifiers)
//!   scroll(x, y, dx, dy)
//!   list_windows()                  -> Vec<WindowInfo>
//!   focus_window(window_id)         -> bool (the window itself was raised)
//!   get_frontmost_window()          -> WindowInfo
//!   read_ax_tree(window_id)         -> JSON string
//!   read_window_text(pid, ...)      -> JSON string (reader mode)
//!   find_ax_element(role, label)    -> Option<Bounds>
//!
//! Every binding that calls macOS releases the GIL while it waits
//! (`py.allow_threads`). Python arguments become Rust values before the
//! call and the Python result is built after it, so nothing inside touches
//! a Python object. A capture, a WindowServer query, an app activation or
//! a long `type_text` that blocks then stalls only the thread that made the
//! call, not the bridge's event loop and every other thread with it.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::Mutex;

mod ax;
mod capture;
mod input;
mod reader;
mod windows;

/// Error converter for the crate: anything we produce as an `anyhow::Error`-ish
/// thing gets mapped to a Python RuntimeError with the message preserved.
fn err<E: std::fmt::Display>(e: E) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

// ─── Screen capture ─────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (display_id=None, max_dim=None, format="png", quality=75))]
fn capture_screen(
    py: Python<'_>,
    display_id: Option<u32>,
    max_dim: Option<u32>,
    format: &str,
    quality: u8,
) -> PyResult<Py<PyBytes>> {
    let fmt = capture::ImageFormat::from_str_with_quality(format, quality);
    let bytes = py
        .allow_threads(|| capture::capture_display(display_id, max_dim, fmt))
        .map_err(err)?;
    Ok(PyBytes::new_bound(py, &bytes).unbind())
}

#[pyfunction]
#[pyo3(signature = (window_id, max_dim=None, format="png", quality=75))]
fn capture_window(
    py: Python<'_>,
    window_id: u32,
    max_dim: Option<u32>,
    format: &str,
    quality: u8,
) -> PyResult<Py<PyBytes>> {
    let fmt = capture::ImageFormat::from_str_with_quality(format, quality);
    let bytes = py
        .allow_threads(|| capture::capture_window(window_id, max_dim, fmt))
        .map_err(err)?;
    Ok(PyBytes::new_bound(py, &bytes).unbind())
}

#[pyfunction]
fn list_displays(py: Python<'_>) -> PyResult<Py<PyList>> {
    let displays = py.allow_threads(capture::list_displays).map_err(err)?;
    let list = PyList::empty_bound(py);
    for d in displays {
        let dict = PyDict::new_bound(py);
        dict.set_item("id", d.id)?;
        dict.set_item("width", d.width)?;
        dict.set_item("height", d.height)?;
        dict.set_item("scale", d.scale)?;
        dict.set_item("is_primary", d.is_primary)?;
        list.append(dict)?;
    }
    Ok(list.unbind())
}

// ─── Input injection ────────────────────────────────────────────────────

/// Held by every input call. While the GIL was held, one input call could
/// not start before the one in progress ended, so the keys of two
/// `type_text` calls from two threads never mixed. Without the GIL this
/// lock keeps that order.
static INPUT: Mutex<()> = Mutex::new(());

/// Run an input call with the GIL released and the input lock held.
fn send_input<T: Send>(py: Python<'_>, f: impl FnOnce() -> T + Send) -> T {
    py.allow_threads(|| {
        // A panic in an earlier call poisons the lock. It guards no data, so
        // it is taken anyway.
        let _held = INPUT.lock().unwrap_or_else(|e| e.into_inner());
        f()
    })
}

#[pyfunction]
#[pyo3(signature = (x, y, button="left", double=false, modifiers=None))]
fn click(
    py: Python<'_>,
    x: i32,
    y: i32,
    button: &str,
    double: bool,
    modifiers: Option<Vec<String>>,
) -> PyResult<()> {
    let button = button.to_owned();
    let modifiers = modifiers.unwrap_or_default();
    send_input(py, || input::click(x, y, &button, double, modifiers)).map_err(err)
}

#[pyfunction]
fn move_mouse(py: Python<'_>, x: i32, y: i32) -> PyResult<()> {
    send_input(py, || input::move_mouse(x, y)).map_err(err)
}

#[pyfunction]
fn type_text(py: Python<'_>, text: &str) -> PyResult<()> {
    let text = text.to_owned();
    send_input(py, || input::type_text(&text)).map_err(err)
}

#[pyfunction]
#[pyo3(signature = (key, modifiers=None))]
fn press_key(py: Python<'_>, key: &str, modifiers: Option<Vec<String>>) -> PyResult<()> {
    let key = key.to_owned();
    let modifiers = modifiers.unwrap_or_default();
    send_input(py, || input::press_key(&key, modifiers)).map_err(err)
}

#[pyfunction]
fn key_down(py: Python<'_>, key: &str) -> PyResult<()> {
    let key = key.to_owned();
    send_input(py, || input::key_down(&key)).map_err(err)
}

#[pyfunction]
fn key_up(py: Python<'_>, key: &str) -> PyResult<()> {
    let key = key.to_owned();
    send_input(py, || input::key_up(&key)).map_err(err)
}

#[pyfunction]
#[pyo3(signature = (dx, dy, x=None, y=None))]
fn scroll(py: Python<'_>, dx: i32, dy: i32, x: Option<i32>, y: Option<i32>) -> PyResult<()> {
    send_input(py, || input::scroll(dx, dy, x, y)).map_err(err)
}

// A read, so it does not wait for the input lock.
#[pyfunction]
fn cursor_position(py: Python<'_>) -> PyResult<(i32, i32)> {
    py.allow_threads(input::cursor_position).map_err(err)
}

// ─── Windows ────────────────────────────────────────────────────────────

#[pyfunction]
fn list_windows(py: Python<'_>) -> PyResult<Py<PyList>> {
    let infos = py.allow_threads(windows::list_windows).map_err(err)?;
    let list = PyList::empty_bound(py);
    for w in infos {
        let dict = PyDict::new_bound(py);
        dict.set_item("id", w.id)?;
        dict.set_item("pid", w.pid)?;
        dict.set_item("bundle", w.bundle)?;
        dict.set_item("owner", w.owner)?;
        dict.set_item("title", w.title)?;
        dict.set_item("bounds", (w.x, w.y, w.w, w.h))?;
        dict.set_item("is_frontmost", w.is_frontmost)?;
        dict.set_item("layer", w.layer)?;
        list.append(dict)?;
    }
    Ok(list.unbind())
}

#[pyfunction]
fn get_frontmost_window(py: Python<'_>) -> PyResult<Option<Py<PyDict>>> {
    let Some(w) = py.allow_threads(windows::get_frontmost_window).map_err(err)? else {
        return Ok(None);
    };
    let dict = PyDict::new_bound(py);
    dict.set_item("id", w.id)?;
    dict.set_item("pid", w.pid)?;
    dict.set_item("bundle", w.bundle)?;
    dict.set_item("owner", w.owner)?;
    dict.set_item("title", w.title)?;
    dict.set_item("bounds", (w.x, w.y, w.w, w.h))?;
    dict.set_item("is_frontmost", true)?;
    dict.set_item("layer", w.layer)?;
    Ok(Some(dict.unbind()))
}

// Raising the window asks the app over AX, so the GIL is released as for
// the AX calls below. True when the window itself was raised, false when
// only its app was activated. Builds before this returned None.
#[pyfunction]
fn focus_window(py: Python<'_>, window_id: u32) -> PyResult<bool> {
    py.allow_threads(|| windows::focus_window(window_id)).map_err(err)
}

#[pyfunction]
fn focus_app(py: Python<'_>, bundle_id: &str) -> PyResult<()> {
    let bundle_id = bundle_id.to_owned();
    py.allow_threads(|| windows::focus_app(&bundle_id)).map_err(err)
}

// ─── Accessibility tree ─────────────────────────────────────────────────

// Every AX call below releases the GIL. They block on the target app, and
// holding the GIL froze the whole Python process (event loop, every
// sub-agent, the UI bridge) for as long as the app took to answer. On
// 2026-10-06 one read of Arc held it for 2.4 hours.
#[pyfunction]
#[pyo3(signature = (pid, max_depth=8, budget_ms=8000, max_nodes=6000))]
fn read_ax_tree(
    py: Python<'_>,
    pid: i32,
    max_depth: usize,
    budget_ms: u64,
    max_nodes: usize,
) -> PyResult<String> {
    py.allow_threads(|| ax::read_ax_tree(pid, max_depth, budget_ms, max_nodes))
        .map_err(err)
}

/// Text of one window in reading order, as JSON (see `reader.rs`). Appshots
/// use it. `window_frame` is the window's CoreGraphics bounds, which picks the
/// matching AX window. Releases the GIL like the other AX reads.
#[pyfunction]
#[pyo3(signature = (pid, window_frame=None, window_title=None, budget_ms=3000, max_chars=24000, max_nodes=8000, enable_web=true))]
#[allow(clippy::too_many_arguments)]
fn read_window_text(
    py: Python<'_>,
    pid: i32,
    window_frame: Option<(f64, f64, f64, f64)>,
    window_title: Option<String>,
    budget_ms: u64,
    max_chars: usize,
    max_nodes: usize,
    enable_web: bool,
) -> PyResult<String> {
    let opts = reader::ReadOptions {
        budget_ms,
        max_chars,
        max_nodes,
        window_frame,
        window_title,
        enable_web,
    };
    py.allow_threads(|| reader::read_window_text(pid, opts))
        .map_err(err)
}

/// Stop every running `read_window_text` at its next node. Only sets a flag,
/// so it keeps the GIL.
#[pyfunction]
fn cancel_window_reads() {
    reader::cancel_reads();
}

#[pyfunction]
fn ax_press(
    py: Python<'_>,
    pid: i32,
    x: f64,
    y: f64,
    role: &str,
    bounds: (f64, f64, f64, f64),
) -> PyResult<bool> {
    py.allow_threads(|| ax::press_at(pid, x, y, role, bounds))
        .map_err(err)
}

#[pyfunction]
fn ax_perform(
    py: Python<'_>,
    pid: i32,
    x: f64,
    y: f64,
    role: &str,
    bounds: (f64, f64, f64, f64),
    action: &str,
) -> PyResult<bool> {
    py.allow_threads(|| ax::perform_at(pid, x, y, role, bounds, action))
        .map_err(err)
}

#[pyfunction]
#[pyo3(signature = (pid, role=None, label=None, title=None))]
fn find_ax_element(
    py: Python<'_>,
    pid: i32,
    role: Option<&str>,
    label: Option<&str>,
    title: Option<&str>,
) -> PyResult<Option<(f64, f64, f64, f64)>> {
    py.allow_threads(|| ax::find_ax_element(pid, role, label, title))
        .map_err(err)
}

// The permission checks ask the system's privacy service (tccd), and the
// Python wrapper runs one before every input call and every capture.
#[pyfunction]
fn check_accessibility_permission(py: Python<'_>) -> PyResult<bool> {
    Ok(py.allow_threads(ax::check_accessibility_permission))
}

#[pyfunction]
fn prompt_accessibility_permission(py: Python<'_>) -> PyResult<bool> {
    Ok(py.allow_threads(ax::prompt_accessibility_permission))
}

#[pyfunction]
fn check_screen_recording_permission(py: Python<'_>) -> PyResult<bool> {
    Ok(py.allow_threads(capture::check_screen_recording_permission))
}

#[pyfunction]
fn prompt_screen_recording_permission(py: Python<'_>) -> PyResult<bool> {
    Ok(py.allow_threads(capture::prompt_screen_recording_permission))
}

/// Entry point — exposes every Python-visible function.
#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // capture
    m.add_function(wrap_pyfunction!(capture_screen, m)?)?;
    m.add_function(wrap_pyfunction!(capture_window, m)?)?;
    m.add_function(wrap_pyfunction!(list_displays, m)?)?;
    m.add_function(wrap_pyfunction!(check_screen_recording_permission, m)?)?;
    m.add_function(wrap_pyfunction!(prompt_screen_recording_permission, m)?)?;
    // input
    m.add_function(wrap_pyfunction!(click, m)?)?;
    m.add_function(wrap_pyfunction!(move_mouse, m)?)?;
    m.add_function(wrap_pyfunction!(type_text, m)?)?;
    m.add_function(wrap_pyfunction!(press_key, m)?)?;
    m.add_function(wrap_pyfunction!(key_down, m)?)?;
    m.add_function(wrap_pyfunction!(key_up, m)?)?;
    m.add_function(wrap_pyfunction!(scroll, m)?)?;
    m.add_function(wrap_pyfunction!(cursor_position, m)?)?;
    // windows
    m.add_function(wrap_pyfunction!(list_windows, m)?)?;
    m.add_function(wrap_pyfunction!(get_frontmost_window, m)?)?;
    m.add_function(wrap_pyfunction!(focus_window, m)?)?;
    m.add_function(wrap_pyfunction!(focus_app, m)?)?;
    // ax
    m.add_function(wrap_pyfunction!(read_ax_tree, m)?)?;
    m.add_function(wrap_pyfunction!(find_ax_element, m)?)?;
    m.add_function(wrap_pyfunction!(read_window_text, m)?)?;
    m.add_function(wrap_pyfunction!(cancel_window_reads, m)?)?;
    m.add_function(wrap_pyfunction!(ax_press, m)?)?;
    m.add_function(wrap_pyfunction!(ax_perform, m)?)?;
    m.add_function(wrap_pyfunction!(check_accessibility_permission, m)?)?;
    m.add_function(wrap_pyfunction!(prompt_accessibility_permission, m)?)?;
    Ok(())
}
