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
    kAXErrorCannotComplete, kAXErrorInvalidUIElement, kAXErrorSuccess, kAXValueTypeAXError,
    kAXValueTypeCGRect, AXIsProcessTrustedWithOptions, AXUIElementCopyElementAtPosition,
    AXUIElementCopyMultipleAttributeValues, AXUIElementRef, AXUIElementSetMessagingTimeout,
    AXValueGetType, AXValueGetTypeID, AXValueGetValue, AXValueRef,
};
use core_foundation::array::{CFArray, CFArrayRef};
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
    /// AXSelected, kept only when true: the chosen option among buttons that
    /// carry no value (System Settings' Light/Dark/Auto), the selected row or tab.
    #[serde(skip_serializing_if = "Option::is_none")]
    selected: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bounds: Option<[f64; 4]>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    children: Vec<AxNode>,
    /// Children left out because they are scrolled out of view and the
    /// parent already listed OFFSCREEN_SIBLINGS such children.
    #[serde(skip_serializing_if = "is_zero")]
    omitted: usize,
    /// Set on the root when the walk hit its time or node budget, so the
    /// tree is a prefix of the real one.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    truncated: bool,
}

fn is_zero(n: &usize) -> bool {
    *n == 0
}

/// What the walk reads for every node, in one round trip to the app
/// (AXUIElementCopyMultipleAttributeValues). One call per attribute cost a
/// dozen round trips per node: Finder in column view spent the whole time
/// budget on about 900 nodes (2026-10-08).
const NODE_ATTRS: [&str; 13] = [
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXRoleDescription",
    "AXHelp",
    "AXIdentifier",
    "AXEnabled",
    "AXFocused",
    "AXFrame",
    "AXSelected",
    "AXChildren",
];
const A_ROLE: usize = 0;
const A_SUBROLE: usize = 1;
const A_TITLE: usize = 2;
const A_DESCRIPTION: usize = 3;
const A_VALUE: usize = 4;
const A_ROLE_DESCRIPTION: usize = 5;
const A_HELP: usize = 6;
const A_IDENTIFIER: usize = 7;
const A_ENABLED: usize = 8;
const A_FOCUSED: usize = 9;
const A_FRAME: usize = 10;
const A_SELECTED: usize = 11;
const A_CHILDREN: usize = 12;

/// Children outside the visible rect of their window and scroll areas that a
/// parent still lists in full. A folder with hundreds of files, a long mail
/// list or a chat history otherwise spends the budget on rows nobody can see,
/// and the content in view is cut off. The ones kept tell the operator what
/// scrolling reveals; a whole column scrolled sideways out of view (Finder)
/// is still read, since it is one of few siblings.
const OFFSCREEN_SIBLINGS: usize = 30;

type Rect = (f64, f64, f64, f64);

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
    let names: Vec<CFString> = NODE_ATTRS.iter().map(|n| CFString::new(n)).collect();
    let names = CFArray::from_CFTypes(&names);
    let (mut node, _) = walk(&root, max_depth, &mut budget, &names, None);
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
    perform_at(pid, x, y, role, frame, "AXPress")
}

/// Perform `action` on the `role` element of app `pid` whose frame is `frame`,
/// found as in `press_at`. Scroll areas take AXScrollDownByPage and its
/// siblings: an exact page, with no pointer movement. Returns false when there
/// is no such element or it does not offer the action.
pub fn perform_at(
    pid: i32,
    x: f64,
    y: f64,
    role: &str,
    frame: (f64, f64, f64, f64),
    action: &str,
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
            return Ok(perform(&elem, action));
        }
        match elem.parent() {
            Ok(parent) => elem = parent,
            Err(_) => break,
        }
    }
    // The hit test lands on whatever is on top, e.g. another window of the same
    // app covering a sheet. Find the element in the tree instead; act on it only
    // if exactly one element has this role and frame.
    match find_unique(&app, role, frame) {
        Some(elem) => Ok(perform(&elem, action)),
        None => Ok(false),
    }
}

fn is_match(elem: &AXUIElement, role: &str, frame: (f64, f64, f64, f64)) -> bool {
    elem.role().map(|r| r.to_string() == role).unwrap_or(false)
        && frame_matches(frame_of(elem), frame)
}

fn perform(elem: &AXUIElement, action: &str) -> bool {
    let offered = elem
        .action_names()
        .map(|names| names.iter().any(|n| n.to_string() == action))
        .unwrap_or(false);
    if !offered {
        return false;
    }
    let done = elem.perform_action(&CFString::new(action)).is_ok();
    // Finder scrolls its column browser by a page and still returns
    // kAXErrorAttributeUnsupported (2026-10-08). A scroll that was offered
    // counts as done; the caller sees its effect on the next read.
    done || action.starts_with("AXScroll")
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

/// Returns the node and whether it lies outside `clip`, the visible rect of
/// its window and enclosing scroll areas.
fn walk(
    elem: &AXUIElement,
    depth: usize,
    budget: &mut Budget,
    names: &CFArray<CFString>,
    clip: Option<Rect>,
) -> (AxNode, bool) {
    let vals = read_attrs(elem, names);
    let get = |i: usize| vals.get(i).and_then(|v| v.as_ref());
    let role = get(A_ROLE).and_then(cf_string).unwrap_or_else(|| "Unknown".into());
    let frame = get(A_FRAME).and_then(cf_rect);
    let outside = is_outside(frame, clip);
    let mut node = AxNode {
        role,
        subrole: get(A_SUBROLE).and_then(cf_string),
        title: get(A_TITLE).and_then(cf_string),
        label: get(A_DESCRIPTION).and_then(cf_string),
        value: get(A_VALUE).and_then(cf_value),
        description: get(A_ROLE_DESCRIPTION).and_then(cf_string),
        help: get(A_HELP).and_then(cf_string),
        identifier: get(A_IDENTIFIER).and_then(cf_string),
        enabled: get(A_ENABLED).and_then(cf_bool),
        focused: get(A_FOCUSED).and_then(cf_bool),
        selected: get(A_SELECTED).and_then(cf_bool).filter(|s| *s),
        bounds: frame.map(|(x, y, w, h)| [x, y, w, h]),
        children: Vec::new(),
        omitted: 0,
        truncated: false,
    };
    let kids = get(A_CHILDREN).map(cf_elements).unwrap_or_default();
    // A closed menu still lists every item (with zero-size frames); in Finder
    // the closed menus were 353 of 382 nodes. Open menus have a real frame.
    let closed_menu = node.role == "AXMenu" && !frame.is_some_and(has_area);
    // Out of budget: keep this node's own attributes cheap by not descending.
    if kids.is_empty() || closed_menu || depth == 0 || !budget.spend() {
        return (node, outside);
    }
    let clip = match node.role.as_str() {
        "AXWindow" | "AXSheet" | "AXPopover" => frame.or(clip),
        "AXScrollArea" => intersect(clip, frame),
        _ => clip,
    };
    let mut offscreen = 0;
    for c in &kids {
        if budget.exhausted || Instant::now() >= budget.deadline {
            budget.exhausted = true;
            break;
        }
        if offscreen >= OFFSCREEN_SIBLINGS && is_outside(frame_of(c), clip) {
            // Past the cap a frame read decides; rows in view can follow many
            // rows above it, so the scan goes on.
            node.omitted += 1;
            continue;
        }
        let (child, out) = walk(c, depth - 1, budget, names, clip);
        if out {
            offscreen += 1;
        }
        node.children.push(child);
    }
    (node, outside)
}

/// The NODE_ATTRS values of `elem`, None where it has no such attribute.
fn read_attrs(elem: &AXUIElement, names: &CFArray<CFString>) -> Vec<Option<CFType>> {
    let mut out: CFArrayRef = std::ptr::null();
    let err = unsafe {
        AXUIElementCopyMultipleAttributeValues(
            elem.as_concrete_TypeRef(),
            names.as_concrete_TypeRef(),
            0,
            &mut out,
        )
    };
    if err == kAXErrorSuccess && !out.is_null() {
        let arr: CFArray<CFType> = unsafe { CFArray::wrap_under_create_rule(out) };
        return arr
            .iter()
            .map(|v| {
                let v: CFType = (*v).clone();
                Some(v).filter(|v| !is_ax_error(v))
            })
            .collect();
    }
    if err == kAXErrorCannotComplete || err == kAXErrorInvalidUIElement {
        // The app did not answer in time, or the element is gone: asking
        // again per attribute would wait out the timeout a dozen times.
        return Vec::new();
    }
    NODE_ATTRS
        .iter()
        .map(|n| elem.attribute(&AXAttribute::<CFType>::new(&CFString::new(n))).ok())
        .collect()
}

/// Missing attributes come back as an AXValue holding an AXError.
fn is_ax_error(v: &CFType) -> bool {
    unsafe {
        v.type_of() == AXValueGetTypeID()
            && AXValueGetType(v.as_CFTypeRef() as AXValueRef) == kAXValueTypeAXError
    }
}

fn cf_string(v: &CFType) -> Option<String> {
    v.downcast::<CFString>().map(|s| s.to_string())
}

fn cf_bool(v: &CFType) -> Option<bool> {
    if let Some(b) = v.downcast::<CFBoolean>() {
        return Some(bool::from(b));
    }
    v.downcast::<CFNumber>().and_then(|n| n.to_i64()).map(|i| i != 0)
}

/// AXValue as text. Checkboxes, radio buttons, switches, and sliders report a
/// CFBoolean or CFNumber rather than a string, so reading strings only lost
/// their state.
fn cf_value(v: &CFType) -> Option<String> {
    if let Some(s) = v.downcast::<CFString>() {
        Some(s.to_string())
    } else if let Some(b) = v.downcast::<CFBoolean>() {
        Some(if bool::from(b) { "1" } else { "0" }.to_string())
    } else if let Some(n) = v.downcast::<CFNumber>() {
        n.to_i64()
            .map(|i| i.to_string())
            .or_else(|| n.to_f64().map(|f| f.to_string()))
    } else {
        None
    }
}

fn cf_rect(v: &CFType) -> Option<Rect> {
    unsafe {
        if v.type_of() != AXValueGetTypeID() {
            return None;
        }
        let value_ref = v.as_CFTypeRef() as AXValueRef;
        if AXValueGetType(value_ref) != kAXValueTypeCGRect {
            return None;
        }
        let mut rect = CGRect::new(&CGPoint::new(0.0, 0.0), &CGSize::new(0.0, 0.0));
        if !AXValueGetValue(value_ref, kAXValueTypeCGRect, &mut rect as *mut _ as *mut c_void) {
            return None;
        }
        Some((rect.origin.x, rect.origin.y, rect.size.width, rect.size.height))
    }
}

fn cf_elements(v: &CFType) -> Vec<AXUIElement> {
    if !v.instance_of::<CFArray>() {
        return Vec::new();
    }
    let arr: CFArray<CFType> = unsafe { CFArray::wrap_under_get_rule(v.as_CFTypeRef() as CFArrayRef) };
    arr.iter()
        .filter(|c| c.type_of() == AXUIElement::type_id())
        .map(|c| unsafe { AXUIElement::wrap_under_get_rule(c.as_CFTypeRef() as AXUIElementRef) })
        .collect()
}

fn has_area(r: Rect) -> bool {
    r.2 > 0.0 && r.3 > 0.0
}

fn intersect(clip: Option<Rect>, frame: Option<Rect>) -> Option<Rect> {
    let Some(f) = frame else { return clip };
    let Some(c) = clip else { return Some(f) };
    let (x0, y0) = (c.0.max(f.0), c.1.max(f.1));
    let (x1, y1) = ((c.0 + c.2).min(f.0 + f.2), (c.1 + c.3).min(f.1 + f.3));
    Some((x0, y0, (x1 - x0).max(0.0), (y1 - y0).max(0.0)))
}

/// True when `frame` has an area and does not overlap `clip`. Elements
/// without a frame, or with an empty one, count as visible: some containers
/// report no size while their children do.
fn is_outside(frame: Option<Rect>, clip: Option<Rect>) -> bool {
    match (frame, clip) {
        (Some(f), Some(c)) if has_area(f) => {
            f.0 + f.2 <= c.0 || c.0 + c.2 <= f.0 || f.1 + f.3 <= c.1 || c.1 + c.3 <= f.1
        }
        _ => false,
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

/// Read AXFrame as (x, y, w, h): an AXValue wrapping a CGRect.
fn frame_of(elem: &AXUIElement) -> Option<(f64, f64, f64, f64)> {
    let attr = AXAttribute::<CFType>::new(&CFString::from_static_string("AXFrame"));
    cf_rect(&elem.attribute(&attr).ok()?)
}

// silence unused-import warning when only cfg(cell) paths are touched
#[allow(dead_code)]
fn _touch() {
    let _ = CFArray::<CFType>::from_CFTypes(&[]);
}
