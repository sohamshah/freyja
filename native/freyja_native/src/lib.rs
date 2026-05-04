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
//!   focus_window(window_id)
//!   get_frontmost_window()          -> WindowInfo
//!   read_ax_tree(window_id)         -> JSON string
//!   find_ax_element(role, label)    -> Option<Bounds>

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

mod ax;
mod capture;
mod input;
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
    let bytes = capture::capture_display(display_id, max_dim, fmt).map_err(err)?;
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
    let bytes = capture::capture_window(window_id, max_dim, fmt).map_err(err)?;
    Ok(PyBytes::new_bound(py, &bytes).unbind())
}

#[pyfunction]
fn list_displays(py: Python<'_>) -> PyResult<Py<PyList>> {
    let displays = capture::list_displays().map_err(err)?;
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

#[pyfunction]
#[pyo3(signature = (x, y, button="left", double=false, modifiers=None))]
fn click(
    x: i32,
    y: i32,
    button: &str,
    double: bool,
    modifiers: Option<Vec<String>>,
) -> PyResult<()> {
    input::click(x, y, button, double, modifiers.unwrap_or_default()).map_err(err)
}

#[pyfunction]
fn move_mouse(x: i32, y: i32) -> PyResult<()> {
    input::move_mouse(x, y).map_err(err)
}

#[pyfunction]
fn type_text(text: &str) -> PyResult<()> {
    input::type_text(text).map_err(err)
}

#[pyfunction]
#[pyo3(signature = (key, modifiers=None))]
fn press_key(key: &str, modifiers: Option<Vec<String>>) -> PyResult<()> {
    input::press_key(key, modifiers.unwrap_or_default()).map_err(err)
}

#[pyfunction]
fn key_down(key: &str) -> PyResult<()> {
    input::key_down(key).map_err(err)
}

#[pyfunction]
fn key_up(key: &str) -> PyResult<()> {
    input::key_up(key).map_err(err)
}

#[pyfunction]
#[pyo3(signature = (dx, dy, x=None, y=None))]
fn scroll(dx: i32, dy: i32, x: Option<i32>, y: Option<i32>) -> PyResult<()> {
    input::scroll(dx, dy, x, y).map_err(err)
}

#[pyfunction]
fn cursor_position() -> PyResult<(i32, i32)> {
    input::cursor_position().map_err(err)
}

// ─── Windows ────────────────────────────────────────────────────────────

#[pyfunction]
fn list_windows(py: Python<'_>) -> PyResult<Py<PyList>> {
    let infos = windows::list_windows().map_err(err)?;
    let list = PyList::empty_bound(py);
    for w in infos {
        let dict = PyDict::new_bound(py);
        dict.set_item("id", w.id)?;
        dict.set_item("pid", w.pid)?;
        dict.set_item("bundle", w.bundle)?;
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
    let Some(w) = windows::get_frontmost_window().map_err(err)? else {
        return Ok(None);
    };
    let dict = PyDict::new_bound(py);
    dict.set_item("id", w.id)?;
    dict.set_item("pid", w.pid)?;
    dict.set_item("bundle", w.bundle)?;
    dict.set_item("title", w.title)?;
    dict.set_item("bounds", (w.x, w.y, w.w, w.h))?;
    dict.set_item("is_frontmost", true)?;
    dict.set_item("layer", w.layer)?;
    Ok(Some(dict.unbind()))
}

#[pyfunction]
fn focus_window(window_id: u32) -> PyResult<()> {
    windows::focus_window(window_id).map_err(err)
}

#[pyfunction]
fn focus_app(bundle_id: &str) -> PyResult<()> {
    windows::focus_app(bundle_id).map_err(err)
}

// ─── Accessibility tree ─────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (pid, max_depth=8))]
fn read_ax_tree(pid: i32, max_depth: usize) -> PyResult<String> {
    ax::read_ax_tree(pid, max_depth).map_err(err)
}

#[pyfunction]
#[pyo3(signature = (pid, role=None, label=None, title=None))]
fn find_ax_element(
    pid: i32,
    role: Option<&str>,
    label: Option<&str>,
    title: Option<&str>,
) -> PyResult<Option<(f64, f64, f64, f64)>> {
    ax::find_ax_element(pid, role, label, title).map_err(err)
}

#[pyfunction]
fn check_accessibility_permission() -> PyResult<bool> {
    Ok(ax::check_accessibility_permission())
}

#[pyfunction]
fn prompt_accessibility_permission() -> PyResult<bool> {
    Ok(ax::prompt_accessibility_permission())
}

#[pyfunction]
fn check_screen_recording_permission() -> PyResult<bool> {
    Ok(capture::check_screen_recording_permission())
}

#[pyfunction]
fn prompt_screen_recording_permission() -> PyResult<bool> {
    Ok(capture::prompt_screen_recording_permission())
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
    m.add_function(wrap_pyfunction!(check_accessibility_permission, m)?)?;
    m.add_function(wrap_pyfunction!(prompt_accessibility_permission, m)?)?;
    Ok(())
}
