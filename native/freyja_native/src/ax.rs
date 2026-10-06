//! macOS Accessibility tree walker.
//!
//! Uses the `accessibility` crate which wraps the AX API. Built-in
//! attribute accessors (`.role()`, `.title()`, `.children()`, ...) come
//! from the `AXUIElementAttributes` trait. For attributes the crate
//! doesn't expose by name (`AXFrame`, `AXDescription`, `AXIdentifier`,
//! `AXHelp`, ...) we construct an `AXAttribute::<CFType>::new(name)`
//! and parse the returned CFType ourselves.

use accessibility::{AXAttribute, AXUIElement, AXUIElementAttributes};
use accessibility_sys::{
    kAXErrorSuccess, kAXValueTypeCGRect, AXIsProcessTrustedWithOptions,
    AXUIElementCopyElementAtPosition, AXUIElementRef, AXUIElementSetMessagingTimeout,
    AXValueGetType, AXValueGetValue, AXValueRef,
};
use core_foundation::array::CFArray;
use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::dictionary::CFDictionary;
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
use serde::Serialize;
use std::ffi::c_void;
use std::time::{Duration, Instant};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AxError {
    #[error("AX error: {0}")]
    Generic(String),
}

impl From<accessibility::Error> for AxError {
    fn from(e: accessibility::Error) -> Self {
        AxError::Generic(e.to_string())
    }
}

#[derive(Serialize, Debug, Clone)]
pub struct AxNode {
    role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    subrole: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    help: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    identifier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    enabled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    focused: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bounds: Option<[f64; 4]>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    children: Vec<AxNode>,
    /// Set on the root when the walk hit its time or node budget, so the
    /// tree is a prefix of the real one.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    truncated: bool,
}

/// Limits for one tree walk. A busy browser such as Arc exposes tens of
/// thousands of nodes, and every node costs about a dozen AX calls that can
/// each wait out the messaging timeout. Without a budget one read took 100+
/// seconds, and once 2.4 hours (2026-10-06).
struct Budget {
    deadline: Instant,
    nodes_left: usize,
    exhausted: bool,
}

impl Budget {
    fn spend(&mut self) -> bool {
        if self.exhausted {
            return false;
        }
        if self.nodes_left == 0 || Instant::now() >= self.deadline {
            self.exhausted = true;
            return false;
        }
        self.nodes_left -= 1;
        true
    }
}

/// How long one AX call may wait on a busy app. The system default is 6 s.
const AX_MESSAGING_TIMEOUT_SECS: f32 = 1.5;

/// Check whether our process currently has Accessibility permission.
/// Uses the non-prompting variant.
pub fn check_accessibility_permission() -> bool {
    unsafe {
        let key = CFString::from_static_string("AXTrustedCheckOptionPrompt");
        let value = CFBoolean::false_value();
        let dict = CFDictionary::from_CFType_pairs(&[(key.as_CFType(), value.as_CFType())]);
        AXIsProcessTrustedWithOptions(dict.as_concrete_TypeRef())
    }
}

/// Check Accessibility permission and, if missing, trigger macOS to
/// pop the "grant Accessibility" dialog for this process. Returns
/// the CURRENT state (before the user's response). The prompt is
/// non-blocking — the user has to go to System Settings → Privacy
/// & Security → Accessibility and toggle the entry on. Calling this
/// again after they've done so will return true.
pub fn prompt_accessibility_permission() -> bool {
    unsafe {
        let key = CFString::from_static_string("AXTrustedCheckOptionPrompt");
        let value = CFBoolean::true_value();
        let dict = CFDictionary::from_CFType_pairs(&[(key.as_CFType(), value.as_CFType())]);
        AXIsProcessTrustedWithOptions(dict.as_concrete_TypeRef())
    }
}

pub fn read_ax_tree(
    pid: i32,
    max_depth: usize,
    budget_ms: u64,
    max_nodes: usize,
) -> Result<String, AxError> {
    let root = AXUIElement::application(pid);
    unsafe {
        AXUIElementSetMessagingTimeout(root.as_concrete_TypeRef(), AX_MESSAGING_TIMEOUT_SECS);
    }
    let mut budget = Budget {
        deadline: Instant::now() + Duration::from_millis(budget_ms),
        nodes_left: max_nodes,
        exhausted: false,
    };
    let mut node = walk(&root, max_depth, &mut budget);
    node.truncated = budget.exhausted;
    serde_json::to_string(&node).map_err(|e| AxError::Generic(e.to_string()))
}

/// Press the `role` element of app `pid` whose frame is `frame`, found by
/// hit-testing (x, y), with the AXPress action instead of a synthetic click.
/// The app hit-tests its own windows, so a window of another app on top cannot
/// receive the press, and controls that ignore synthetic mouse events still
/// respond. The hit is usually a child of the control (its text or image), so
/// this climbs to the nearest ancestor with the expected role and frame.
/// Returns false when there is no such element or it has no AXPress action.
pub fn press_at(
    pid: i32,
    x: f64,
    y: f64,
    role: &str,
    frame: (f64, f64, f64, f64),
) -> Result<bool, AxError> {
    let app = AXUIElement::application(pid);
    let mut hit: AXUIElementRef = std::ptr::null_mut();
    let err = unsafe {
        AXUIElementCopyElementAtPosition(app.as_concrete_TypeRef(), x as f32, y as f32, &mut hit)
    };
    if err != kAXErrorSuccess || hit.is_null() {
        return Ok(false);
    }
    let mut elem = unsafe { AXUIElement::wrap_under_create_rule(hit) };
    for _ in 0..8 {
        if is_match(&elem, role, frame) {
            return Ok(press(&elem));
        }
        match elem.parent() {
            Ok(parent) => elem = parent,
            Err(_) => break,
        }
    }
    // The hit test lands on whatever is on top, e.g. another window of the same
    // app covering a sheet. Find the element in the tree instead; press it only
    // if exactly one element has this role and frame.
    match find_unique(&app, role, frame) {
        Some(elem) => Ok(press(&elem)),
        None => Ok(false),
    }
}

fn is_match(elem: &AXUIElement, role: &str, frame: (f64, f64, f64, f64)) -> bool {
    elem.role().map(|r| r.to_string() == role).unwrap_or(false)
        && frame_matches(frame_of(elem), frame)
}

fn press(elem: &AXUIElement) -> bool {
    let pressable = elem
        .action_names()
        .map(|names| names.iter().any(|n| n.to_string() == "AXPress"))
        .unwrap_or(false);
    pressable
        && elem
            .perform_action(&CFString::from_static_string("AXPress"))
            .is_ok()
}

fn find_unique(
    root: &AXUIElement,
    role: &str,
    frame: (f64, f64, f64, f64),
) -> Option<AXUIElement> {
    let mut stack: Vec<AXUIElement> = vec![root.clone()];
    let mut found: Option<AXUIElement> = None;
    let mut seen = 0;
    while let Some(elem) = stack.pop() {
        seen += 1;
        if seen > 4000 {
            return None;
        }
        if is_match(&elem, role, frame) {
            if found.is_some() {
                return None;
            }
            found = Some(elem.clone());
        }
        if let Ok(children) = elem.children() {
            stack.extend(children.iter().map(|c| c.clone()));
        }
    }
    found
}

fn frame_matches(actual: Option<(f64, f64, f64, f64)>, want: (f64, f64, f64, f64)) -> bool {
    actual.is_some_and(|a| {
        (a.0 - want.0).abs() <= 2.0
            && (a.1 - want.1).abs() <= 2.0
            && (a.2 - want.2).abs() <= 2.0
            && (a.3 - want.3).abs() <= 2.0
    })
}

pub fn find_ax_element(
    pid: i32,
    role: Option<&str>,
    label: Option<&str>,
    title: Option<&str>,
) -> Result<Option<(f64, f64, f64, f64)>, AxError> {
    let root = AXUIElement::application(pid);
    let mut stack: Vec<AXUIElement> = vec![root];
    let max_nodes = 4000;
    let mut seen = 0;
    while let Some(elem) = stack.pop() {
        seen += 1;
        if seen > max_nodes {
            break;
        }

        let matches_role = role
            .map(|r| {
                elem.role()
                    .ok()
                    .map(|s| s.to_string() == r)
                    .unwrap_or(false)
            })
            .unwrap_or(true);
        let matches_label = label
            .map(|l| {
                let cand = custom_string(&elem, "AXDescription")
                    .or_else(|| elem.title().ok().map(|s| s.to_string()))
                    .or_else(|| custom_string(&elem, "AXValue"));
                cand.as_deref()
                    .map(|c| c.eq_ignore_ascii_case(l) || c.contains(l))
                    .unwrap_or(false)
            })
            .unwrap_or(true);
        let matches_title = title
            .map(|t| {
                elem.title()
                    .ok()
                    .map(|s| {
                        let s = s.to_string();
                        s.eq_ignore_ascii_case(t) || s.contains(t)
                    })
                    .unwrap_or(false)
            })
            .unwrap_or(true);

        if matches_role && matches_label && matches_title {
            if let Some(bounds) = frame_of(&elem) {
                return Ok(Some(bounds));
            }
        }

        if let Ok(children) = elem.children() {
            // CFArrayIterator isn't DoubleEndedIterator — collect first.
            let as_vec: Vec<AXUIElement> = children.iter().map(|c| c.clone()).collect();
            for c in as_vec.into_iter().rev() {
                stack.push(c);
            }
        }
    }
    Ok(None)
}

fn walk(elem: &AXUIElement, depth: usize, budget: &mut Budget) -> AxNode {
    // Out of budget: keep this node's own attributes cheap by not descending.
    let children: Vec<AxNode> = if depth == 0 || !budget.spend() {
        Vec::new()
    } else {
        match elem.children() {
            Ok(arr) => {
                let mut out = Vec::new();
                for c in arr.iter() {
                    if budget.exhausted {
                        break;
                    }
                    out.push(walk(&c, depth - 1, budget));
                }
                out
            }
            Err(_) => Vec::new(),
        }
    };
    AxNode {
        role: elem
            .role()
            .ok()
            .map(|s| s.to_string())
            .unwrap_or_else(|| "Unknown".into()),
        subrole: elem.subrole().ok().map(|s| s.to_string()),
        title: elem.title().ok().map(|s| s.to_string()),
        label: custom_string(elem, "AXDescription"),
        value: value_string(elem),
        description: elem.role_description().ok().map(|s| s.to_string()),
        help: elem.help().ok().map(|s| s.to_string()),
        identifier: elem.identifier().ok().map(|s| s.to_string()),
        enabled: elem.enabled().ok().map(|b| b == CFBoolean::true_value()),
        focused: elem.focused().ok().map(|b| b == CFBoolean::true_value()),
        bounds: frame_of(elem).map(|(x, y, w, h)| [x, y, w, h]),
        children,
        truncated: false,
    }
}

/// Fetch an arbitrary string attribute by name.
fn custom_string(elem: &AXUIElement, name: &str) -> Option<String> {
    let attr = AXAttribute::<CFType>::new(&CFString::new(name));
    let value = elem.attribute(&attr).ok()?;
    // The returned CFType might be a CFString directly, or wrap an
    // AXValue. We only care about CFString for descriptions/values.
    if value.instance_of::<CFString>() {
        let s = unsafe {
            CFString::wrap_under_get_rule(value.as_CFTypeRef() as _)
        };
        Some(s.to_string())
    } else {
        None
    }
}

/// AXValue as text. Checkboxes, radio buttons, switches, and sliders report a
/// CFBoolean or CFNumber rather than a string, so reading strings only lost
/// their state.
fn value_string(elem: &AXUIElement) -> Option<String> {
    let attr = AXAttribute::<CFType>::new(&CFString::new("AXValue"));
    let value = elem.attribute(&attr).ok()?;
    if value.instance_of::<CFString>() {
        let s = unsafe { CFString::wrap_under_get_rule(value.as_CFTypeRef() as _) };
        Some(s.to_string())
    } else if value.instance_of::<CFBoolean>() {
        let b = unsafe { CFBoolean::wrap_under_get_rule(value.as_CFTypeRef() as _) };
        Some(if bool::from(b) { "1" } else { "0" }.to_string())
    } else if value.instance_of::<CFNumber>() {
        let n = unsafe { CFNumber::wrap_under_get_rule(value.as_CFTypeRef() as _) };
        n.to_i64()
            .map(|i| i.to_string())
            .or_else(|| n.to_f64().map(|f| f.to_string()))
    } else {
        None
    }
}

/// Read AXFrame as (x, y, w, h). AXFrame's value is an AXValue wrapping
/// a CGRect. We pull it out via AXValueGetValue.
fn frame_of(elem: &AXUIElement) -> Option<(f64, f64, f64, f64)> {
    let attr = AXAttribute::<CFType>::new(&CFString::from_static_string("AXFrame"));
    let value = elem.attribute(&attr).ok()?;
    let value_ref = value.as_CFTypeRef() as AXValueRef;
    if value_ref.is_null() {
        return None;
    }
    unsafe {
        let kind = AXValueGetType(value_ref);
        if kind != kAXValueTypeCGRect {
            return None;
        }
        let mut rect = CGRect::new(&CGPoint::new(0.0, 0.0), &CGSize::new(0.0, 0.0));
        let ok = AXValueGetValue(value_ref, kind, &mut rect as *mut _ as *mut c_void);
        if !ok {
            return None;
        }
        Some((rect.origin.x, rect.origin.y, rect.size.width, rect.size.height))
    }
}

// silence unused-import warning when only cfg(cell) paths are touched
#[allow(dead_code)]
fn _touch() {
    let _ = CFArray::<CFType>::from_CFTypes(&[]);
}
