//! Input injection via Enigo (CGEvent under the hood on macOS).
//!
//! We build a fresh Enigo instance per call because (a) the library is
//! cheap to construct, (b) stale instances can get into weird modifier
//! states if the user is using the keyboard at the same time, and (c)
//! this crate is stateless by design — all "state" lives in Python.
//!
//! Coordinate system: Quartz uses top-left origin, which is what the
//! agent operates in too. Enigo respects that on macOS.

use enigo::{
    Axis, Button, Coordinate, Direction, Enigo, Key, Keyboard, Mouse, Settings,
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
    let btn = parse_button(button)?;
    let mut enigo = make_enigo()?;
    enigo
        .move_mouse(x, y, Coordinate::Abs)
        .map_err(|e| InputError::Action(e.to_string()))?;
    with_modifiers(&mut enigo, &modifiers, |e| {
        e.button(btn, Direction::Click)
            .map_err(|err| InputError::Action(err.to_string()))?;
        if double {
            e.button(btn, Direction::Click)
                .map_err(|err| InputError::Action(err.to_string()))?;
        }
        Ok(())
    })
}

/// Move the mouse without clicking.
pub fn move_mouse(x: i32, y: i32) -> Result<(), InputError> {
    let mut enigo = make_enigo()?;
    enigo
        .move_mouse(x, y, Coordinate::Abs)
        .map_err(|e| InputError::Action(e.to_string()))
}

/// Type a string. Honors whatever keyboard layout is active.
pub fn type_text(text: &str) -> Result<(), InputError> {
    let mut enigo = make_enigo()?;
    enigo
        .text(text)
        .map_err(|e| InputError::Action(e.to_string()))
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
    let mut enigo = make_enigo()?;
    enigo
        .key(k, Direction::Press)
        .map_err(|e| InputError::Action(e.to_string()))
}

/// Release a previously-held key.
pub fn key_up(key: &str) -> Result<(), InputError> {
    let k = parse_key(key)?;
    let mut enigo = make_enigo()?;
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
    let mut enigo = make_enigo()?;
    if let (Some(x), Some(y)) = (x, y) {
        enigo
            .move_mouse(x, y, Coordinate::Abs)
            .map_err(|e| InputError::Action(e.to_string()))?;
    }
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
