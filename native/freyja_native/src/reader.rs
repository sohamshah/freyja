//! Reader mode: the text of one window, in reading order.
//!
//! The acting walk in `ax.rs` returns a JSON tree with bounds and identifiers,
//! built for clicking. This walk returns plain text, built for a person or a
//! model to read ("what is on this window?"). It starts at one window, not at
//! the application, keeps text that is scrolled out of view, and writes each
//! table row as one line.
//!
//! Appshots use it: one gesture sends an image of the window plus this text.

use crate::ax::{
    cf_bool, cf_elements, cf_rect, cf_string, cf_value, check_accessibility_permission, frame_of,
    has_area, intersect, is_outside, read_attrs, AxError, Budget, Rect, AX_MESSAGING_TIMEOUT_SECS,
};
use accessibility::{AXAttribute, AXUIElement};
use accessibility_sys::{
    kAXErrorSuccess, kAXValueTypeCFRange, AXUIElementRef, AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout, AXValueGetType, AXValueGetTypeID, AXValueGetValue, AXValueRef,
};
use core_foundation::array::CFArray;
use core_foundation::base::{CFRange, CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::string::CFString;
use core_foundation::url::CFURL;
use serde::Serialize;
use std::ffi::c_void;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// Bumped at the start of every read and by `cancel_reads`. A walk that sees a
/// newer number than its own stops at the next node, so a new capture does not
/// wait for an old one that is stuck on a slow app.
static GENERATION: AtomicU64 = AtomicU64::new(0);

pub fn cancel_reads() {
    GENERATION.fetch_add(1, Ordering::SeqCst);
}

const READ_ATTRS: [&str; 11] = [
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXFrame",
    "AXChildren",
    "AXSelected",
    "AXPlaceholderValue",
    "AXURL",
    "AXVisibleCharacterRange",
];
const R_ROLE: usize = 0;
const R_SUBROLE: usize = 1;
const R_TITLE: usize = 2;
const R_DESCRIPTION: usize = 3;
const R_VALUE: usize = 4;
const R_FRAME: usize = 5;
const R_CHILDREN: usize = 6;
const R_SELECTED: usize = 7;
const R_PLACEHOLDER: usize = 8;
const R_URL: usize = 9;
const R_VISIBLE_RANGE: usize = 10;

/// Longest text of one field (a document, a terminal's scrollback) that goes
/// into the output whole. A longer one is cut around its visible part.
const FIELD_CAP: usize = 12_000;
/// Longest single line of any other node.
const LINE_CAP: usize = 1_500;
const MAX_DEPTH: usize = 48;
/// Rows outside the visible area that one parent may still list. The acting
/// walk keeps 30. Reading wants more, because text scrolled out of view is the
/// point, but a mail list with 20,000 rows must not use the whole budget.
const OFFSCREEN_READ_CAP: usize = 120;
/// The same limit inside a web page. A page lists everything in one long
/// column, so the text below the fold is most of the page, and the character
/// and node limits already bound the read.
const WEB_OFFSCREEN_READ_CAP: usize = 1_500;
/// A pass with fewer lines than this inside web areas looks like an app whose
/// web content is not exposed yet (Chromium and Electron build their tree on
/// request). The count is for web content only, because a browser's own
/// sidebar and toolbar give more lines than this by themselves.
const SPARSE_LINES: usize = 6;
/// A web area that covers at least this share of its window is the page of a
/// browser (or of an app built like one), and its text comes first.
const PAGE_SHARE: f64 = 0.6;
/// Lines of the window's own controls kept after the page text.
const CHROME_LINES_KEPT: usize = 25;
/// Characters a Chromium app's own controls (a sidebar, a toolbar) may use. They
/// have this budget of their own, so a long sidebar cannot use up the page's:
/// Arc listed 488 sidebar lines before the page.
const CHROME_CHARS: usize = 6_000;
const SELECTED_TEXT_CAP: usize = 4_000;
/// How long a read waits for a Chromium or Electron app to build its web tree.
const WEB_WAIT_MS: u64 = 3_000;

/// Roles that only draw chrome and carry no text worth reading.
const SKIP_ROLES: [&str; 6] = [
    "AXScrollBar",
    "AXValueIndicator",
    "AXSplitter",
    "AXGrowArea",
    "AXRulerMarker",
    "AXIncrementor",
];

pub struct ReadOptions {
    pub budget_ms: u64,
    pub max_chars: usize,
    pub max_nodes: usize,
    /// Frame of the target window as CoreGraphics reports it. The AX window
    /// with the same frame is the one read.
    pub window_frame: Option<Rect>,
    pub window_title: Option<String>,
    /// Ask a Chromium or Electron app to build its web accessibility tree when
    /// the first pass finds almost nothing, and put the setting back after.
    pub enable_web: bool,
}

#[derive(Serialize, Default)]
pub struct ReadResult {
    pub text: String,
    /// `ok`, `partial` or `unavailable`.
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub truncated: bool,
    pub chars: usize,
    pub nodes: usize,
    pub ms: u64,
    /// How the window was found: `frame`, `title`, `focused`, `main`, `first`
    /// or `none`.
    pub window_matched: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub window_title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focused: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_url: Option<String>,
    /// True when this read switched web accessibility on for the app.
    pub web_enabled: bool,
    /// Items left out because they lie outside the view and their parent had
    /// more than the read allows. The text says where, and the status is
    /// `partial`.
    pub omitted: usize,
}

struct Pass {
    lines: Vec<String>,
    /// Lines that came from inside a web area.
    web_lines: usize,
    omitted: usize,
    nodes: usize,
    truncated: bool,
    cancelled: bool,
    page_url: Option<String>,
    window_matched: &'static str,
    window_title: Option<String>,
    focused: Option<String>,
    selected_text: Option<String>,
    no_window: bool,
}

pub fn read_window_text(pid: i32, opts: ReadOptions) -> Result<String, AxError> {
    let started = Instant::now();
    let my_gen = GENERATION.fetch_add(1, Ordering::SeqCst) + 1;
    let mut result = ReadResult {
        window_matched: "none".into(),
        ..Default::default()
    };
    if !check_accessibility_permission() {
        result.status = "unavailable".into();
        result.reason = Some("Accessibility permission is not granted to Freyja".into());
        return to_json(&result);
    }
    let app = AXUIElement::application(pid);
    unsafe {
        AXUIElementSetMessagingTimeout(app.as_concrete_TypeRef(), AX_MESSAGING_TIMEOUT_SECS);
    }
    let names: Vec<CFString> = READ_ATTRS.iter().map(|n| CFString::new(n)).collect();
    let names = CFArray::from_CFTypes(&names);
    let deadline = Instant::now() + Duration::from_millis(opts.budget_ms);

    let chromium = is_chromium_app(pid);
    let chrome_cap = chromium.then_some(CHROME_CHARS.min(opts.max_chars));
    let mut pass = read_once(&app, &opts, &names, my_gen, deadline, chrome_cap);
    // Chromium and Electron apps build their web tree only when something asks
    // for it. A first pass that found almost no web text is the sign. A browser
    // shows plenty of its own controls even when the page is missing, so the
    // count is of lines inside web areas. Electron takes a setting that turns
    // the tree on. Chrome and the browsers built on it take none, and start
    // building the tree when the first pass queries them. Either way, wait for
    // the tree and read again, then put the setting back, so Freyja leaves no
    // app in accessibility mode (which slows it down) after the read.
    if opts.enable_web && !pass.cancelled && !pass.no_window && pass.web_lines < SPARSE_LINES {
        let switch = enable_web_tree(&app);
        if switch.is_some() || chromium {
            result.web_enabled = switch.is_some();
            // The tree takes from a few hundred milliseconds to a couple of
            // seconds to appear, so read again until it does. The first
            // pass may have used the whole budget, so this wait has its own.
            let web_deadline = Instant::now() + Duration::from_millis(WEB_WAIT_MS);
            let mut wait = 250;
            loop {
                std::thread::sleep(Duration::from_millis(wait));
                let next = read_once(&app, &opts, &names, my_gen, web_deadline, chrome_cap);
                let done = next.web_lines >= SPARSE_LINES || next.cancelled;
                if (next.web_lines, next.lines.len()) >= (pass.web_lines, pass.lines.len()) {
                    pass = next;
                }
                if done || Instant::now() >= web_deadline {
                    break;
                }
                wait = (wait * 2).min(800);
            }
            if let Some(switch) = switch {
                set_bool_attr(&app, switch, false);
            }
        }
    }

    result.nodes = pass.nodes;
    result.window_matched = pass.window_matched.into();
    result.window_title = pass.window_title;
    result.focused = pass.focused;
    result.selected_text = pass.selected_text;
    result.page_url = pass.page_url;
    result.truncated = pass.truncated;
    result.omitted = pass.omitted;
    result.text = pass.lines.join("\n");
    result.chars = result.text.chars().count();
    result.ms = started.elapsed().as_millis() as u64;
    if pass.cancelled {
        result.status = "unavailable".into();
        result.reason = Some("replaced by a newer capture".into());
    } else if pass.no_window {
        result.status = "unavailable".into();
        result.reason = Some("the app lists no accessibility window".into());
    } else if pass.lines.is_empty() {
        result.status = "unavailable".into();
        result.reason = Some(
            "the window exposes no text through accessibility (a canvas or custom drawn view)"
                .into(),
        );
    } else if pass.truncated || pass.omitted > 0 {
        result.status = "partial".into();
        result.reason = Some(partial_reason(pass.truncated, pass.omitted));
    } else {
        result.status = "ok".into();
    }
    to_json(&result)
}

/// Why a read is `partial`. Both causes can apply to one read.
fn partial_reason(stopped: bool, omitted: usize) -> String {
    let items = if omitted == 1 { "item" } else { "items" };
    match (stopped, omitted) {
        (true, 0) => "the read stopped at its time or size limit".into(),
        (false, n) => format!("{n} {items} outside the view were not read"),
        (true, n) => format!(
            "the read stopped at its time or size limit, and {n} {items} outside the view were not read"
        ),
    }
}

fn to_json(r: &ReadResult) -> Result<String, AxError> {
    serde_json::to_string(r).map_err(|e| AxError::Generic(e.to_string()))
}

fn read_once(
    app: &AXUIElement,
    opts: &ReadOptions,
    names: &CFArray<CFString>,
    my_gen: u64,
    deadline: Instant,
    chrome_cap: Option<usize>,
) -> Pass {
    let mut pass = Pass {
        lines: Vec::new(),
        web_lines: 0,
        omitted: 0,
        nodes: 0,
        truncated: false,
        cancelled: false,
        page_url: None,
        window_matched: "none",
        window_title: None,
        focused: None,
        selected_text: None,
        no_window: false,
    };
    let (window, matched) = find_window(app, opts);
    let Some(window) = window else {
        pass.no_window = true;
        return pass;
    };
    pass.window_matched = matched;
    pass.window_title = string_attr(&window, "AXTitle").filter(|t| !t.is_empty());
    let (focused, selected) = focus_info(app, &window);
    pass.focused = focused;
    pass.selected_text = selected;

    let mut walk = Walk {
        names,
        budget: Budget {
            deadline,
            nodes_left: opts.max_nodes,
            exhausted: false,
        },
        my_gen,
        out: Vec::new(),
        in_page: Vec::new(),
        web_depth: 0,
        page_area: 0.0,
        omitted: 0,
        omitted_in_page: 0,
        chrome_cap,
        chrome_chars: 0,
        chrome_dropped: false,
        rows: Vec::new(),
        chars: 0,
        max_chars: opts.max_chars,
        nodes: 0,
        truncated: false,
        cancelled: false,
        stop: false,
        heading: false,
        page_url: None,
    };
    walk.visit(&window, MAX_DEPTH, None);
    pass.web_lines = walk.in_page.iter().filter(|p| **p).count();
    let window_area = frame_of(&window).map(|f| f.2 * f.3).unwrap_or(0.0);
    let page_share = if window_area > 0.0 { walk.page_area / window_area } else { 0.0 };
    let (lines, page_first) = arrange(walk.out, &walk.in_page, page_share);
    pass.lines = lines;
    // With the page first, only what the page lost makes the read partial: the
    // window's own controls are cut to a short list anyway.
    pass.omitted = if page_first { walk.omitted_in_page } else { walk.omitted };
    pass.nodes = walk.nodes;
    pass.truncated = walk.truncated || (!page_first && walk.chrome_dropped);
    pass.cancelled = walk.cancelled;
    pass.page_url = walk.page_url;
    pass
}

/// Put the page first. A browser window lists its sidebar and toolbar before
/// the page, and the page is what the reader asked about. When a web area fills
/// most of the window (`page_share`), the lines inside it come first, then a
/// short list of the window's own controls. Any other window keeps its order,
/// because a mail list beside a message body is content too. The flag says
/// whether the page was put first.
fn arrange(lines: Vec<String>, in_page: &[bool], page_share: f64) -> (Vec<String>, bool) {
    let page = in_page.iter().filter(|p| **p).count();
    if page == 0 || page == lines.len() || page_share < PAGE_SHARE {
        return (lines, false);
    }
    let (page_lines, chrome): (Vec<_>, Vec<_>) = lines
        .into_iter()
        .zip(in_page.iter().copied())
        .partition(|(_, p)| *p);
    let mut out: Vec<String> = page_lines.into_iter().map(|(l, _)| l).collect();
    out.push(String::new());
    out.push("Window controls outside the page:".into());
    let total = chrome.len();
    out.extend(chrome.into_iter().take(CHROME_LINES_KEPT).map(|(l, _)| l));
    if total > CHROME_LINES_KEPT {
        out.push(format!(
            "[{} more window controls were not listed]",
            total - CHROME_LINES_KEPT
        ));
    }
    (out, true)
}

// ─── Finding the window and the focus ───────────────────────────────────

fn near(actual: Option<Rect>, want: Rect) -> bool {
    actual.is_some_and(|a| {
        (a.0 - want.0).abs() <= 4.0
            && (a.1 - want.1).abs() <= 4.0
            && (a.2 - want.2).abs() <= 4.0
            && (a.3 - want.3).abs() <= 4.0
    })
}

fn find_window(app: &AXUIElement, opts: &ReadOptions) -> (Option<AXUIElement>, &'static str) {
    let windows = elements_attr(app, "AXWindows");
    if let Some(want) = opts.window_frame {
        if let Some(w) = windows.iter().find(|w| near(frame_of(w), want)) {
            return (Some(w.clone()), "frame");
        }
    }
    if let Some(title) = opts.window_title.as_deref().filter(|t| !t.is_empty()) {
        if let Some(w) = windows
            .iter()
            .find(|w| string_attr(w, "AXTitle").as_deref() == Some(title))
        {
            return (Some(w.clone()), "title");
        }
    }
    if let Some(w) = element_attr(app, "AXFocusedWindow") {
        return (Some(w), "focused");
    }
    if let Some(w) = element_attr(app, "AXMainWindow") {
        return (Some(w), "main");
    }
    match windows.into_iter().next() {
        Some(w) => (Some(w), "first"),
        None => (None, "none"),
    }
}

/// What has the keyboard focus and what is selected there, only when the focus
/// is inside the window being read.
fn focus_info(app: &AXUIElement, window: &AXUIElement) -> (Option<String>, Option<String>) {
    let Some(focus) = element_attr(app, "AXFocusedUIElement") else {
        return (None, None);
    };
    let inside = element_attr(&focus, "AXWindow")
        .map(|w| w.as_CFType() == window.as_CFType())
        .unwrap_or(false);
    if !inside {
        return (None, None);
    }
    let role = string_attr(&focus, "AXRole").unwrap_or_default();
    let label = ["AXTitle", "AXDescription", "AXPlaceholderValue"]
        .iter()
        .find_map(|a| string_attr(&focus, a).filter(|s| !s.trim().is_empty()));
    let described = match label {
        Some(l) => format!("{} \"{}\"", role.trim_start_matches("AX"), clean_line(&l)),
        None => role.trim_start_matches("AX").to_string(),
    };
    let selected = string_attr(&focus, "AXSelectedText")
        .map(|s| clean_block(&s))
        .filter(|s| !s.trim().is_empty())
        .map(|s| cut_chars(&s, SELECTED_TEXT_CAP));
    (Some(described), selected)
}

fn attr(elem: &AXUIElement, name: &str) -> Option<CFType> {
    elem.attribute(&AXAttribute::<CFType>::new(&CFString::new(name)))
        .ok()
}

fn string_attr(elem: &AXUIElement, name: &str) -> Option<String> {
    attr(elem, name).and_then(|v| cf_string(&v))
}

fn elements_attr(elem: &AXUIElement, name: &str) -> Vec<AXUIElement> {
    attr(elem, name).map(|v| cf_elements(&v)).unwrap_or_default()
}

fn element_attr(elem: &AXUIElement, name: &str) -> Option<AXUIElement> {
    let v = attr(elem, name)?;
    if v.type_of() != AXUIElement::type_id() {
        return None;
    }
    Some(unsafe { AXUIElement::wrap_under_get_rule(v.as_CFTypeRef() as AXUIElementRef) })
}

// ─── Web accessibility switch (Chromium, Electron) ──────────────────────

/// The settings that ask an app to build its web accessibility tree.
/// Electron and some Chromium builds take `AXManualAccessibility`. Chrome and
/// the browsers built on it take `AXEnhancedUserInterface`, which is what
/// VoiceOver sets.
const WEB_SWITCHES: [&str; 2] = ["AXManualAccessibility", "AXEnhancedUserInterface"];

/// Turn the web tree on with the first setting the app accepts, and return its
/// name so the caller can turn it off again. Returns None when a setting is
/// already on (somebody else wants it left alone) or the app accepts none,
/// which is the case for native apps.
fn enable_web_tree(app: &AXUIElement) -> Option<&'static str> {
    if WEB_SWITCHES
        .iter()
        .any(|name| attr(app, name).and_then(|v| cf_bool(&v)) == Some(true))
    {
        return None;
    }
    WEB_SWITCHES
        .into_iter()
        .find(|name| set_bool_attr(app, name, true))
}

fn set_bool_attr(app: &AXUIElement, name: &str, on: bool) -> bool {
    let name = CFString::new(name);
    let value = if on {
        CFBoolean::true_value()
    } else {
        CFBoolean::false_value()
    };
    unsafe {
        AXUIElementSetAttributeValue(
            app.as_concrete_TypeRef(),
            name.as_concrete_TypeRef(),
            value.as_CFTypeRef(),
        ) == kAXErrorSuccess
    }
}

/// True when the app is built on Chromium: Electron apps, Chrome and the
/// browsers made from it. Every one ships a renderer helper app, either beside
/// its frameworks (Electron) or inside one of them (Chrome, Arc, Edge).
fn is_chromium_app(pid: i32) -> bool {
    use objc2_app_kit::NSRunningApplication;
    use std::path::{Path, PathBuf};

    fn has_renderer_helper(dir: &Path) -> bool {
        std::fs::read_dir(dir).is_ok_and(|entries| {
            entries
                .flatten()
                .any(|e| e.file_name().to_string_lossy().ends_with("Helper (Renderer).app"))
        })
    }

    let bundle: Option<PathBuf> = unsafe {
        NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
            .and_then(|app| app.bundleURL())
            .and_then(|url| url.path())
            .map(|p| PathBuf::from(p.to_string()))
    };
    let Some(frameworks) = bundle.map(|b| b.join("Contents/Frameworks")) else {
        return false;
    };
    if has_renderer_helper(&frameworks) {
        return true;
    }
    let Ok(entries) = std::fs::read_dir(&frameworks) else {
        return false;
    };
    entries.flatten().any(|fw| {
        fw.path().extension().is_some_and(|x| x == "framework")
            && std::fs::read_dir(fw.path().join("Versions"))
                .is_ok_and(|versions| versions.flatten().any(|v| has_renderer_helper(&v.path().join("Helpers"))))
    })
}

// ─── The walk ───────────────────────────────────────────────────────────

struct Walk<'a> {
    names: &'a CFArray<CFString>,
    budget: Budget,
    my_gen: u64,
    out: Vec<String>,
    /// For each line of `out`: true when it came from inside a web area.
    in_page: Vec<bool>,
    /// How many web areas the walk is inside (a page can hold frames).
    web_depth: usize,
    /// The largest area of a web area seen, in points squared.
    page_area: f64,
    /// Items skipped because their parent held more offscreen items than the
    /// read allows, everywhere and inside web areas.
    omitted: usize,
    omitted_in_page: usize,
    /// The budget of a Chromium app's own controls (see CHROME_CHARS), None for
    /// other apps, whose every line counts against `max_chars`.
    chrome_cap: Option<usize>,
    chrome_chars: usize,
    chrome_dropped: bool,
    /// Open table rows. Text found inside a row joins its collector, and the
    /// row is written as one line when it closes.
    rows: Vec<Vec<String>>,
    chars: usize,
    max_chars: usize,
    nodes: usize,
    truncated: bool,
    cancelled: bool,
    stop: bool,
    /// Set inside a heading, so its first line gets a "# " prefix.
    heading: bool,
    page_url: Option<String>,
}

impl Walk<'_> {
    fn halted(&mut self) -> bool {
        if self.stop {
            return true;
        }
        if GENERATION.load(Ordering::SeqCst) != self.my_gen {
            self.cancelled = true;
            self.stop = true;
            return true;
        }
        if self.budget.exhausted || Instant::now() >= self.budget.deadline {
            self.budget.exhausted = true;
            self.truncated = true;
            self.stop = true;
            return true;
        }
        false
    }

    fn emit(&mut self, line: String) {
        let mut line = line;
        if line.trim().is_empty() {
            return;
        }
        if self.heading {
            self.heading = false;
            line = format!("# {line}");
        }
        if let Some(row) = self.rows.last_mut() {
            if row.last() != Some(&line) {
                row.push(line);
            }
            return;
        }
        if self.out.last() == Some(&line) {
            return;
        }
        let used = line.chars().count() + 1;
        if let (0, Some(cap)) = (self.web_depth, self.chrome_cap) {
            if self.chrome_chars + used > cap {
                self.chrome_dropped = true;
            } else {
                self.chrome_chars += used;
                self.out.push(line);
                self.in_page.push(false);
            }
            return;
        }
        if self.chars + used > self.max_chars {
            let room = self.max_chars.saturating_sub(self.chars);
            if room > 40 {
                self.out.push(format!("{}…", cut_chars(&line, room - 1)));
                self.in_page.push(self.web_depth > 0);
            }
            self.truncated = true;
            self.stop = true;
            return;
        }
        self.chars += used;
        self.out.push(line);
        self.in_page.push(self.web_depth > 0);
    }

    /// Visit `elem`. Returns true when the element lies outside the visible
    /// area of its window and scroll areas, so the parent can count it.
    fn visit(&mut self, elem: &AXUIElement, depth: usize, clip: Option<Rect>) -> bool {
        if self.halted() {
            return false;
        }
        if !self.budget.spend() {
            self.truncated = true;
            self.stop = true;
            return false;
        }
        self.nodes += 1;
        let vals = read_attrs(elem, self.names, &READ_ATTRS);
        let get = |i: usize| vals.get(i).and_then(|v| v.as_ref());
        let role = get(R_ROLE).and_then(cf_string).unwrap_or_default();
        let subrole = get(R_SUBROLE).and_then(cf_string).unwrap_or_default();
        let frame = get(R_FRAME).and_then(cf_rect);
        // Chromium gives an element that is scrolled out of view a frame of
        // zero size at the edge of the page, so inside a web area a frame with
        // no area means "out of view", and its text is part of the page.
        let clipped_in_page = self.web_depth > 0 && frame.is_some_and(|f| !has_area(f));
        let outside = is_outside(frame, clip) || clipped_in_page;
        // Elsewhere a leaf with a frame but no area is not drawn. Containers can
        // report no size while their children have one, so they are still
        // entered.
        let hidden = frame.is_some_and(|f| !has_area(f)) && !clipped_in_page;
        if SKIP_ROLES.contains(&role.as_str()) {
            return outside;
        }
        let nonempty = |s: Option<String>| s.map(|s| clean_line(&s)).filter(|s| !s.is_empty());
        let title = nonempty(get(R_TITLE).and_then(cf_string));
        let desc = nonempty(get(R_DESCRIPTION).and_then(cf_string));
        let placeholder = nonempty(get(R_PLACEHOLDER).and_then(cf_string));
        let value_str = get(R_VALUE).and_then(cf_string);
        let value_any = get(R_VALUE).and_then(cf_value);
        let selected = get(R_SELECTED).and_then(cf_bool).unwrap_or(false);
        let label = title.clone().or_else(|| desc.clone());
        let kids = get(R_CHILDREN).map(cf_elements).unwrap_or_default();

        if subrole == "AXSecureTextField" {
            let name = label.or(placeholder).unwrap_or_else(|| "secure field".into());
            self.emit(format!("{name}: [hidden: password field]"));
            return outside;
        }
        // The closed menus of an app list every item with a zero-size frame.
        if role == "AXMenu" && hidden {
            return outside;
        }

        let in_web_area = role == "AXWebArea";
        if in_web_area {
            self.web_depth += 1;
            if let Some(f) = frame {
                self.page_area = self.page_area.max(f.2 * f.3);
            }
        }
        let mut descend = true;
        let mut opened_row = false;
        match role.as_str() {
            "AXStaticText" => {
                if !hidden {
                    let text = value_str.clone().map(|s| clean_line(&s)).filter(|s| !s.is_empty());
                    if let Some(t) = text.or_else(|| title.clone()).or_else(|| desc.clone()) {
                        self.emit(t);
                    }
                }
                descend = false;
            }
            "AXTextField" | "AXTextArea" | "AXSearchField" | "AXComboBox" | "AXTextView" => {
                if !hidden {
                    let range = get(R_VISIBLE_RANGE).and_then(cf_range);
                    let had_text = self.emit_field(
                        label.as_deref(),
                        placeholder.as_deref(),
                        value_str.as_deref(),
                        range,
                    );
                    // A field with a value is complete. Its children (web text
                    // runs) would repeat it.
                    descend = !had_text;
                }
            }
            "AXImage" => {
                if !hidden {
                    if let Some(d) = desc.clone().or_else(|| title.clone()) {
                        if d.chars().count() > 1 {
                            self.emit(format!("[image: {d}]"));
                        }
                    }
                }
                descend = false;
            }
            "AXButton" | "AXMenuButton" | "AXDisclosureTriangle" => {
                if let Some(l) = label.clone() {
                    if !hidden {
                        self.emit(format!("[{l}]"));
                    }
                    descend = false;
                }
            }
            "AXCheckBox" | "AXRadioButton" | "AXSwitch" => {
                if let Some(l) = label.clone() {
                    let on = value_any.as_deref().map(|v| v != "0").unwrap_or(selected);
                    let line = if subrole == "AXTabButton" {
                        format!("[tab] {l}{}", if on { " *" } else { "" })
                    } else if role == "AXRadioButton" {
                        format!("({}) {l}", if on { "•" } else { " " })
                    } else {
                        format!("[{}] {l}", if on { "x" } else { " " })
                    };
                    if !hidden {
                        self.emit(line);
                    }
                    descend = false;
                }
            }
            "AXPopUpButton" | "AXSlider" => {
                if let Some(l) = label.clone() {
                    let line = match value_any.as_deref().filter(|v| !v.is_empty()) {
                        Some(v) => format!("{l}: {v}"),
                        None => l,
                    };
                    if !hidden {
                        self.emit(line);
                    }
                    descend = false;
                } else if let Some(v) = value_any.clone().filter(|v| !v.is_empty()) {
                    if !hidden {
                        self.emit(v);
                    }
                    descend = false;
                }
            }
            "AXLink" => {
                if let Some(l) = label.clone() {
                    if !hidden {
                        self.emit(format!("[link] {l}"));
                    }
                    descend = false;
                }
            }
            "AXMenuItem" => {
                if let Some(l) = title.clone() {
                    if !hidden {
                        self.emit(format!("- {l}"));
                    }
                }
            }
            "AXWebArea" => {
                if self.page_url.is_none() {
                    self.page_url = get(R_URL).and_then(cf_url);
                }
            }
            "AXHeading" => {
                self.heading = true;
            }
            "AXRow" => {
                self.rows.push(Vec::new());
                opened_row = true;
            }
            _ => {
                // A control the walk has no rule for (a cell, a group, a custom
                // view) still shows its text when it has no children.
                if kids.is_empty() && !hidden {
                    let text = value_str
                        .clone()
                        .map(|s| clean_line(&s))
                        .filter(|s| !s.is_empty())
                        .or_else(|| title.clone())
                        .or_else(|| desc.clone());
                    if let Some(t) = text {
                        self.emit(t);
                    }
                }
            }
        }

        if descend && depth > 0 && !kids.is_empty() {
            let clip = match role.as_str() {
                "AXWindow" | "AXSheet" | "AXPopover" => frame.or(clip),
                "AXScrollArea" => intersect(clip, frame),
                _ => clip,
            };
            let cap = if self.web_depth > 0 {
                WEB_OFFSCREEN_READ_CAP
            } else {
                OFFSCREEN_READ_CAP
            };
            let mut offscreen_read = 0;
            let mut omitted = 0;
            for c in &kids {
                if self.halted() {
                    break;
                }
                if offscreen_read >= cap && is_outside(frame_of(c), clip) {
                    omitted += 1;
                    continue;
                }
                if self.visit(c, depth - 1, clip) {
                    offscreen_read += 1;
                }
            }
            if omitted > 0 {
                self.omitted += omitted;
                if self.web_depth > 0 {
                    self.omitted_in_page += omitted;
                }
                if !self.stop {
                    self.emit(format!("[{omitted} more items outside the view were not read]"));
                }
            }
        }
        if in_web_area {
            self.web_depth -= 1;
        }
        if role == "AXHeading" {
            self.heading = false;
        }
        if opened_row {
            let cells = self.rows.pop().unwrap_or_default();
            if !cells.is_empty() {
                let joined = cells.join(" | ");
                self.emit(if selected {
                    format!("(selected) {joined}")
                } else {
                    joined
                });
            }
        }
        outside
    }

    /// Write one text field. Returns true when the field holds text.
    fn emit_field(
        &mut self,
        label: Option<&str>,
        placeholder: Option<&str>,
        value: Option<&str>,
        visible: Option<(usize, usize)>,
    ) -> bool {
        let value = value.map(clean_block).unwrap_or_default();
        if value.trim().is_empty() {
            match (label, placeholder) {
                (Some(l), Some(p)) => self.emit(format!("{l}: (empty, hint \"{p}\")")),
                (Some(l), None) => self.emit(format!("{l}: (empty)")),
                (None, Some(p)) => self.emit(format!("(empty field, hint \"{p}\")")),
                (None, None) => {}
            }
            return false;
        }
        let (text, note) = slice_value(&value, visible, FIELD_CAP);
        let multiline = text.contains('\n') || text.chars().count() > 200;
        match label {
            Some(l) if multiline => {
                self.emit(format!("{l}:"));
                self.emit(text);
            }
            Some(l) => self.emit(format!("{l}: {text}")),
            None => self.emit(text),
        }
        if let Some(n) = note {
            self.emit(n);
        }
        true
    }
}

// ─── Text helpers ───────────────────────────────────────────────────────

fn cf_range(v: &CFType) -> Option<(usize, usize)> {
    unsafe {
        if v.type_of() != AXValueGetTypeID() {
            return None;
        }
        let value_ref = v.as_CFTypeRef() as AXValueRef;
        if AXValueGetType(value_ref) != kAXValueTypeCFRange {
            return None;
        }
        let mut range = CFRange {
            location: 0,
            length: 0,
        };
        if !AXValueGetValue(value_ref, kAXValueTypeCFRange, &mut range as *mut _ as *mut c_void) {
            return None;
        }
        if range.location < 0 || range.length < 0 {
            return None;
        }
        Some((range.location as usize, range.length as usize))
    }
}

fn cf_url(v: &CFType) -> Option<String> {
    v.downcast::<CFURL>().map(|u| u.get_string().to_string())
}

/// Characters that carry no text: zero-width marks, the byte order mark, the
/// object replacement mark that rich text uses for an embedded attachment.
fn is_noise(c: char) -> bool {
    matches!(c, '\u{0}' | '\u{200B}' | '\u{200C}' | '\u{200D}' | '\u{FEFF}' | '\u{FFFC}')
}

/// One line: no noise characters, no line breaks, one space between words.
fn clean_line(s: &str) -> String {
    let joined: String = s.chars().filter(|c| !is_noise(*c)).collect();
    let line = joined.split_whitespace().collect::<Vec<_>>().join(" ");
    cut_chars(&line, LINE_CAP)
}

/// Text that may span lines: no noise characters, `\n` line breaks, no
/// trailing spaces.
fn clean_block(s: &str) -> String {
    let filtered: String = s.chars().filter(|c| !is_noise(*c)).collect();
    filtered
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .lines()
        .map(|l| l.trim_end())
        .collect::<Vec<_>>()
        .join("\n")
}

fn cut_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Cut `value` to `cap` UTF-16 units around the range the user sees. AX ranges
/// count UTF-16 units, as NSString does. Returns the text and a note that
/// says what was left out.
fn slice_value(value: &str, visible: Option<(usize, usize)>, cap: usize) -> (String, Option<String>) {
    let units: Vec<u16> = value.encode_utf16().collect();
    let n = units.len();
    if n <= cap {
        return (value.to_string(), None);
    }
    let (vs, ve) = match visible {
        Some((s, l)) if l > 0 && s + l <= n => (s, s + l),
        _ => (0, cap.min(n)),
    };
    let vis_len = ve - vs;
    let (s, e) = if vis_len >= cap {
        (vs, vs + cap)
    } else {
        // Keep context around the visible part: a third above, two thirds below.
        let extra = cap - vis_len;
        let before = extra / 3;
        let mut s = vs.saturating_sub(before);
        let mut e = (ve + (extra - before)).min(n);
        let got = e - s;
        if got < cap {
            let more = cap - got;
            if s == 0 {
                e = (e + more).min(n);
            } else {
                s = s.saturating_sub(more);
            }
        }
        (s, e)
    };
    let text = String::from_utf16_lossy(&units[s..e]);
    let note = format!("[characters {s} to {e} of {n} shown, the rest of this field is not included]");
    (text, Some(note))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn partial_reason_names_each_cause() {
        assert_eq!(partial_reason(true, 0), "the read stopped at its time or size limit");
        assert_eq!(partial_reason(false, 1), "1 item outside the view were not read");
        assert_eq!(partial_reason(false, 318), "318 items outside the view were not read");
        assert!(partial_reason(true, 5).contains("time or size limit, and 5 items"));
    }

    #[test]
    fn page_comes_first_when_it_fills_the_window() {
        let all = lines(&["[Hide sidebar]", "[Tab one]", "Room rates", "Cancellation policy"]);
        let page = [false, false, true, true];
        let (out, page_first) = arrange(all, &page, 0.75);
        assert!(page_first);
        assert_eq!(out[0], "Room rates");
        assert_eq!(out[1], "Cancellation policy");
        assert_eq!(out[2], "");
        assert_eq!(out[3], "Window controls outside the page:");
        assert_eq!(&out[4..], &["[Hide sidebar]", "[Tab one]"]);
    }

    #[test]
    fn window_controls_are_cut_to_a_short_list_and_counted() {
        let mut all: Vec<String> = (0..60).map(|i| format!("[Tab {i}]")).collect();
        let mut page = vec![false; 60];
        all.push("Room rates".into());
        page.push(true);
        let (out, _) = arrange(all, &page, 0.9);
        assert_eq!(out[0], "Room rates");
        let kept = out.iter().filter(|l| l.starts_with("[Tab ")).count();
        assert_eq!(kept, CHROME_LINES_KEPT);
        assert_eq!(out.last().unwrap(), "[35 more window controls were not listed]");
    }

    #[test]
    fn a_small_web_area_keeps_the_window_order() {
        // A mail list beside a message body: both are content.
        let all = lines(&["Inbox row one", "Inbox row two", "Message body"]);
        let page = [false, false, true];
        assert_eq!(arrange(all.clone(), &page, 0.4), (all, false));
    }

    #[test]
    fn windows_without_web_text_or_with_only_web_text_keep_their_order() {
        let all = lines(&["a", "b"]);
        assert_eq!(arrange(all.clone(), &[false, false], 0.9), (all.clone(), false));
        assert_eq!(arrange(all.clone(), &[true, true], 0.9), (all, false));
    }
}
