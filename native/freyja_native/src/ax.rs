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
    kAXValueTypeCGRect, AXIsProcessTrustedWithOptions, AXValueGetType, AXValueGetValue,
    AXValueRef,
};
use core_foundation::array::CFArray;
use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::dictionary::CFDictionary;
use core_foundation::string::CFString;
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
use serde::Serialize;
use std::ffi::c_void;
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
}

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

pub fn read_ax_tree(pid: i32, max_depth: usize) -> Result<String, AxError> {
    let root = AXUIElement::application(pid);
    let node = walk(&root, max_depth);
    serde_json::to_string(&node).map_err(|e| AxError::Generic(e.to_string()))
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

fn walk(elem: &AXUIElement, depth: usize) -> AxNode {
    let children: Vec<AxNode> = if depth == 0 {
        Vec::new()
    } else {
        match elem.children() {
            Ok(arr) => arr.iter().map(|c| walk(&c, depth - 1)).collect(),
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
        value: custom_string(elem, "AXValue"),
        description: elem.role_description().ok().map(|s| s.to_string()),
        help: elem.help().ok().map(|s| s.to_string()),
        identifier: elem.identifier().ok().map(|s| s.to_string()),
        enabled: elem.enabled().ok().map(|b| b == CFBoolean::true_value()),
        focused: elem.focused().ok().map(|b| b == CFBoolean::true_value()),
        bounds: frame_of(elem).map(|(x, y, w, h)| [x, y, w, h]),
        children,
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
