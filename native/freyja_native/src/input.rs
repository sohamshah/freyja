//! Input injection via Enigo (CGEvent under the hood on macOS).
//!
//! We build a fresh Enigo instance per call because (a) the library is
//! cheap to construct, (b) stale instances can get into weird modifier
//! states if the user is using the keyboard at the same time, and (c)
//! this crate is stateless by design — all "state" lives in Python.
//!
//! Coordinate system: Quartz uses top-left origin, which is what the
//! agent operates in too. Enigo respects that on macOS.

use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTapLocation, CGEventType, CGMouseButton, EventField,
};
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
use core_graphics::geometry::CGPoint;
use std::time::{Duration, Instant};
use enigo::{
    Axis, Button, Direction, Enigo, Key, Keyboard, Mouse, Settings,
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum InputError {
    #[error("enigo init failed: {0}")]
    Init(String),
    #[error("input action failed: {0}")]
    Action(String),
    #[error("unknown button: {0}")]
    UnknownButton(String),
    #[error("unknown key: {0}")]
    UnknownKey(String),
}

fn make_enigo() -> Result<Enigo, InputError> {
    Enigo::new(&Settings::default()).map_err(|e| InputError::Init(e.to_string()))
}

/// For key_down/key_up: the default Enigo releases every key it pressed when
/// dropped, so a fresh instance per call would let go of the key immediately.
fn make_holding_enigo() -> Result<Enigo, InputError> {
    Enigo::new(&Settings {
        release_keys_when_dropped: false,
        ..Settings::default()
    })
    .map_err(|e| InputError::Init(e.to_string()))
}

fn parse_button(button: &str) -> Result<Button, InputError> {
    // Back/Forward aren't exposed on macOS in enigo 0.2 — just the 3 buttons.
    Ok(match button.to_lowercase().as_str() {
        "left" | "l" | "primary" => Button::Left,
        "right" | "r" | "secondary" => Button::Right,
        "middle" | "m" => Button::Middle,
        other => return Err(InputError::UnknownButton(other.to_string())),
    })
}

fn parse_modifier(name: &str) -> Result<Key, InputError> {
    Ok(match name.to_lowercase().as_str() {
        "cmd" | "command" | "meta" | "super" | "win" => Key::Meta,
        "ctrl" | "control" => Key::Control,
        "alt" | "option" | "opt" => Key::Alt,
        "shift" => Key::Shift,
        "fn" => Key::Function,
        other => return Err(InputError::UnknownKey(other.to_string())),
    })
}

/// Parse a named key into an Enigo `Key`. Letters and digits become
/// `Key::Unicode(c)`; named keys (Return, Tab, Escape, …) map onto the
/// enum variants. Arrow keys, function keys, and the common editor keys
/// all work. Multi-char names are case-insensitive.
fn parse_key(key: &str) -> Result<Key, InputError> {
    let k = key.trim();
    if k.chars().count() == 1 {
        return Ok(Key::Unicode(k.chars().next().unwrap()));
    }
    Ok(match k.to_lowercase().as_str() {
        "return" | "enter" | "ret" => Key::Return,
        "tab" => Key::Tab,
        "space" => Key::Space,
        "escape" | "esc" => Key::Escape,
        "backspace" | "back" | "bs" => Key::Backspace,
        "delete" | "del" => Key::Delete,
        "home" => Key::Home,
        "end" => Key::End,
        "pageup" | "page_up" => Key::PageUp,
        "pagedown" | "page_down" => Key::PageDown,
        "up" | "arrow_up" | "up_arrow" => Key::UpArrow,
        "down" | "arrow_down" | "down_arrow" => Key::DownArrow,
        "left" | "arrow_left" | "left_arrow" => Key::LeftArrow,
        "right" | "arrow_right" | "right_arrow" => Key::RightArrow,
        "caps" | "capslock" | "caps_lock" => Key::CapsLock,
        "cmd" | "command" | "meta" | "super" => Key::Meta,
        "ctrl" | "control" => Key::Control,
        "alt" | "option" | "opt" => Key::Alt,
        "shift" => Key::Shift,
        "f1" => Key::F1,
        "f2" => Key::F2,
        "f3" => Key::F3,
        "f4" => Key::F4,
        "f5" => Key::F5,
        "f6" => Key::F6,
        "f7" => Key::F7,
        "f8" => Key::F8,
        "f9" => Key::F9,
        "f10" => Key::F10,
        "f11" => Key::F11,
        "f12" => Key::F12,
        other => return Err(InputError::UnknownKey(other.to_string())),
    })
}

fn with_modifiers<F>(enigo: &mut Enigo, mods: &[String], f: F) -> Result<(), InputError>
where
    F: FnOnce(&mut Enigo) -> Result<(), InputError>,
{
    // Press down each modifier, run the action, release in reverse.
    let keys: Vec<Key> = mods
        .iter()
        .map(|m| parse_modifier(m))
        .collect::<Result<_, _>>()?;
    for k in &keys {
        enigo
            .key(*k, Direction::Press)
            .map_err(|e| InputError::Action(e.to_string()))?;
    }
    let res = f(enigo);
    for k in keys.iter().rev() {
        // Best-effort release — swallow errors so we don't leave a key stuck.
        let _ = enigo.key(*k, Direction::Release);
    }
    res
}

/// Click at absolute screen coordinates.
pub fn click(
    x: i32,
    y: i32,
    button: &str,
    double: bool,
    modifiers: Vec<String>,
) -> Result<(), InputError> {
    let (down, up, cg_button) = mouse_events(button)?;
    let point = CGPoint::new(x as f64, y as f64);
    move_pointer(point)?;
    let flags = modifier_flags(&modifiers)?;
    let mut enigo = make_enigo()?;
    with_modifiers(&mut enigo, &modifiers, |_| {
        for n in 1..=(if double { 2 } else { 1 }) {
            post_mouse(down, point, cg_button, flags, n)?;
            post_mouse(up, point, cg_button, flags, n)?;
        }
        Ok(())
    })
}

/// Move the mouse without clicking.
pub fn move_mouse(x: i32, y: i32) -> Result<(), InputError> {
    move_pointer(CGPoint::new(x as f64, y as f64))
}

// Clicks carry their own location. enigo posts the button events at the
// pointer position it reads back right after posting the move, which is often
// still the old position, so the click landed wherever the pointer had been.
fn post_mouse(
    kind: CGEventType,
    at: CGPoint,
    button: CGMouseButton,
    flags: CGEventFlags,
    click_state: i64,
) -> Result<(), InputError> {
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
        .map_err(|_| InputError::Action("creating an event source failed".into()))?;
    let event = CGEvent::new_mouse_event(source, kind, at, button)
        .map_err(|_| InputError::Action("creating a mouse event failed".into()))?;
    if click_state > 0 {
        event.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_state);
    }
    event.set_flags(flags);
    event.post(CGEventTapLocation::HID);
    Ok(())
}

/// Move the pointer and wait (up to 300 ms) until the system reports it there,
/// so hover state and scroll-wheel routing see the new position.
fn move_pointer(to: CGPoint) -> Result<(), InputError> {
    post_mouse(CGEventType::MouseMoved, to, CGMouseButton::Left, CGEventFlags::CGEventFlagNull, 0)?;
    let deadline = Instant::now() + Duration::from_millis(300);
    while Instant::now() < deadline {
        let here = CGEventSource::new(CGEventSourceStateID::CombinedSessionState)
            .ok()
            .and_then(|src| CGEvent::new(src).ok())
            .map(|e| e.location());
        if let Some(p) = here {
            if (p.x - to.x).abs() < 1.0 && (p.y - to.y).abs() < 1.0 {
                return Ok(());
            }
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    Ok(())
}

fn mouse_events(button: &str) -> Result<(CGEventType, CGEventType, CGMouseButton), InputError> {
    Ok(match parse_button(button)? {
        Button::Right => (CGEventType::RightMouseDown, CGEventType::RightMouseUp, CGMouseButton::Right),
        Button::Middle => (CGEventType::OtherMouseDown, CGEventType::OtherMouseUp, CGMouseButton::Center),
        _ => (CGEventType::LeftMouseDown, CGEventType::LeftMouseUp, CGMouseButton::Left),
    })
}

fn modifier_flags(modifiers: &[String]) -> Result<CGEventFlags, InputError> {
    let mut flags = CGEventFlags::CGEventFlagNull;
    for m in modifiers {
        flags |= match parse_modifier(m)? {
            Key::Meta => CGEventFlags::CGEventFlagCommand,
            Key::Control => CGEventFlags::CGEventFlagControl,
            Key::Alt => CGEventFlags::CGEventFlagAlternate,
            Key::Shift => CGEventFlags::CGEventFlagShift,
            _ => CGEventFlags::CGEventFlagSecondaryFn,
        };
    }
    Ok(flags)
}

/// Type a string. Honors whatever keyboard layout is active.
pub fn type_text(text: &str) -> Result<(), InputError> {
    let mut enigo = make_enigo()?;
    enigo
        .text(text)
        .map_err(|e| InputError::Action(e.to_string()))?;
    // enigo posts each text chunk as a key-down of keycode 0 (the A key) with
    // no key-up, which leaves A reported as held in the HID state.
    release_keycode(0)
}

fn release_keycode(keycode: u16) -> Result<(), InputError> {
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
        .map_err(|_| InputError::Action("creating an event source failed".into()))?;
    let event = CGEvent::new_keyboard_event(source, keycode, false)
        .map_err(|_| InputError::Action("creating a key-up event failed".into()))?;
    event.set_flags(CGEventFlags::CGEventFlagNull);
    event.post(CGEventTapLocation::HID);
    Ok(())
}

/// Press a named key (optionally with modifiers).
pub fn press_key(key: &str, modifiers: Vec<String>) -> Result<(), InputError> {
    let k = parse_key(key)?;
    let mut enigo = make_enigo()?;
    with_modifiers(&mut enigo, &modifiers, |e| {
        e.key(k, Direction::Click)
            .map_err(|err| InputError::Action(err.to_string()))
    })
}

/// Press a key down without releasing. Used together with `key_up`
/// to implement hold-key patterns like ⌘-held-Tab-Tab-Tab app
/// switcher cycling or drag selections with shift held.
///
/// WARNING: every `key_down` MUST be matched by a `key_up` on the
/// same key. A leaked held modifier will pollute the user's entire
/// subsequent session until they quit the offending app.
pub fn key_down(key: &str) -> Result<(), InputError> {
    let k = parse_key(key)?;
    let mut enigo = make_holding_enigo()?;
    enigo
        .key(k, Direction::Press)
        .map_err(|e| InputError::Action(e.to_string()))
}

/// Release a previously-held key.
pub fn key_up(key: &str) -> Result<(), InputError> {
    let k = parse_key(key)?;
    let mut enigo = make_holding_enigo()?;
    enigo
        .key(k, Direction::Release)
        .map_err(|e| InputError::Action(e.to_string()))
}

/// Scroll. If (x,y) is provided, move the cursor there first so the
/// scroll targets the right view. `dy > 0` = scroll down, matching
/// natural direction. `dx > 0` = scroll right.
pub fn scroll(
    dx: i32,
    dy: i32,
    x: Option<i32>,
    y: Option<i32>,
) -> Result<(), InputError> {
    if let (Some(x), Some(y)) = (x, y) {
        move_pointer(CGPoint::new(x as f64, y as f64))?;
    }
    let mut enigo = make_enigo()?;
    if dy != 0 {
        enigo
            .scroll(dy, Axis::Vertical)
            .map_err(|e| InputError::Action(e.to_string()))?;
    }
    if dx != 0 {
        enigo
            .scroll(dx, Axis::Horizontal)
            .map_err(|e| InputError::Action(e.to_string()))?;
    }
    Ok(())
}

/// Current mouse position in global screen coordinates.
pub fn cursor_position() -> Result<(i32, i32), InputError> {
    let enigo = make_enigo()?;
    enigo
        .location()
        .map_err(|e| InputError::Action(e.to_string()))
}
