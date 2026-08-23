//! Input, written straight to the kernel.
//!
//! A virtual input device is created through `uinput` and events are written
//! to it, so they arrive exactly as though a physical keyboard and mouse had
//! produced them. That is what reaches native Wayland windows: there is no X
//! server to inject into, and the compositor trusts the kernel.
//!
//! Two things fall out of doing this natively rather than through ydotool.
//! There is no daemon to keep running - the previous version had to start one
//! and explain itself when it was missing. And the pointer is declared
//! *absolute*, which means the compositor places it where it is told; a
//! relative device is subject to pointer acceleration, which silently doubled
//! every movement and took a calibration pass to work around.

use anyhow::{Context, Result};
use evdev::uinput::{VirtualDevice, VirtualDeviceBuilder};
use evdev::{
    AttributeSet, EventType, InputEvent, KeyCode, KeyEvent, RelativeAxisCode,
};
use std::sync::{Mutex, OnceLock};

struct Devices {
    pointer: VirtualDevice,
    wheel: VirtualDevice,
    keyboard: VirtualDevice,
}

fn devices() -> Result<&'static Mutex<Devices>> {
    static DEVICES: OnceLock<Mutex<Devices>> = OnceLock::new();
    if let Some(ready) = DEVICES.get() {
        return Ok(ready);
    }
    let made = Mutex::new(build().context(
        "could not create a virtual input device; /dev/uinput must be writable \
         (an ACL for your user is enough, no root needed)",
    )?);
    Ok(DEVICES.get_or_init(|| made))
}

fn build() -> Result<Devices> {
    let mut buttons = AttributeSet::<KeyCode>::new();
    for button in [KeyCode::BTN_LEFT, KeyCode::BTN_RIGHT, KeyCode::BTN_MIDDLE] {
        buttons.insert(button);
    }
    // A plain relative mouse, which is the one shape libinput reliably routes
    // to the cursor. Absolute axes were tried first and are the better idea -
    // they would not be subject to pointer acceleration - but libinput reads
    // them as relative deltas unless the device presents as a touchscreen or a
    // full tablet tool, and neither is a small change. See NOTES-rust-port.md.
    let pointer = VirtualDeviceBuilder::new()?
        .name("linux-gui-mcp pointer")
        .with_keys(&buttons)?
        .with_relative_axes(&AttributeSet::from_iter([
            RelativeAxisCode::REL_X,
            RelativeAxisCode::REL_Y,
        ]))?
        .build()?;

    // Every key the kernel knows, so any chord can be pressed without asking
    // first which keys this device claims to have.
    let mut keys = AttributeSet::<KeyCode>::new();
    for code in 1..249u16 {
        keys.insert(KeyCode(code));
    }
    let wheel = VirtualDeviceBuilder::new()?
        .name("linux-gui-mcp wheel")
        .with_relative_axes(&AttributeSet::from_iter([
            RelativeAxisCode::REL_WHEEL,
            RelativeAxisCode::REL_HWHEEL,
        ]))?
        .build()?;

    let keyboard = VirtualDeviceBuilder::new()?
        .name("linux-gui-mcp keyboard")
        .with_keys(&keys)?
        .build()?;

    // A device is not usable the instant it is created: the compositor has to
    // notice it through udev first, and events written before that are simply
    // lost - which looks exactly like the input having done nothing.
    // A second, not the 300ms first tried: the compositor has to notice the
    // device through udev and libinput before it will route anything from it,
    // and events written before that are silently dropped - which looks
    // exactly like the input having done nothing.
    std::thread::sleep(std::time::Duration::from_millis(1000));
    Ok(Devices { pointer, wheel, keyboard })
}

/// Move the pointer by a relative amount.
pub fn move_by(dx: i32, dy: i32) -> Result<()> {
    let mut held = devices()?.lock().unwrap();
    held.pointer.emit(&[
        InputEvent::new(EventType::RELATIVE.0, RelativeAxisCode::REL_X.0, dx),
        InputEvent::new(EventType::RELATIVE.0, RelativeAxisCode::REL_Y.0, dy),
    ])?;
    Ok(())
}

/// Slam the pointer into the top-left corner.
///
/// A large negative move is clamped by the compositor at the corner, which
/// gives a known origin no matter where the pointer started - the only way to
/// reach a known position with a relative device.
pub fn home() -> Result<()> {
    // In steps rather than one enormous delta. libinput discards a jump that
    // large as implausible - a real mouse cannot travel 20000 units in one
    // report - so a single move silently does nothing at all.
    for _ in 0..40 {
        move_by(-250, -250)?;
    }
    Ok(())
}

/// Put the pointer at an absolute position, given the measured scale.
///
/// Pointer acceleration means a relative move of 500 does not travel 500
/// pixels, so the caller measures the factor once and passes it in.
pub fn move_to(x: i32, y: i32, scale: (f64, f64)) -> Result<()> {
    home()?;
    std::thread::sleep(std::time::Duration::from_millis(10));
    if x != 0 || y != 0 {
        move_by(
            (f64::from(x) / scale.0).round() as i32,
            (f64::from(y) / scale.1).round() as i32,
        )?;
    }
    Ok(())
}

fn button(name: &str) -> Result<KeyCode> {
    Ok(match name.to_ascii_lowercase().as_str() {
        "left" => KeyCode::BTN_LEFT,
        "right" => KeyCode::BTN_RIGHT,
        "middle" => KeyCode::BTN_MIDDLE,
        other => anyhow::bail!("unknown button {other:?}; use left, right or middle"),
    })
}

pub fn click(name: &str, count: u32) -> Result<()> {
    let key = button(name)?;
    let mut held = devices()?.lock().unwrap();
    for _ in 0..count.max(1) {
        held.pointer.emit(&[*KeyEvent::new(key, 1)])?;
        held.pointer.emit(&[*KeyEvent::new(key, 0)])?;
        std::thread::sleep(std::time::Duration::from_millis(30));
    }
    Ok(())
}

pub fn press_button(name: &str, down: bool) -> Result<()> {
    let key = button(name)?;
    let mut held = devices()?.lock().unwrap();
    held.pointer.emit(&[*KeyEvent::new(key, i32::from(down))])?;
    Ok(())
}

/// Positive scrolls up, negative down - the direction a wheel turns.
pub fn scroll(amount: i32) -> Result<()> {
    let mut held = devices()?.lock().unwrap();
    held.wheel.emit(&[InputEvent::new(
        EventType::RELATIVE.0,
        RelativeAxisCode::REL_WHEEL.0,
        amount,
    )])?;
    Ok(())
}

/// Press a chord such as "ctrl+shift+k" and let it go again.
///
/// Released in reverse, which is what a keyboard does and what applications
/// expect: a modifier released before the key it modifies reads as two
/// separate presses.
pub fn press_keys(codes: &[u16]) -> Result<()> {
    let mut held = devices()?.lock().unwrap();
    for code in codes {
        held.keyboard.emit(&[*KeyEvent::new(KeyCode(*code), 1)])?;
    }
    for code in codes.iter().rev() {
        held.keyboard.emit(&[*KeyEvent::new(KeyCode(*code), 0)])?;
    }
    Ok(())
}

/// Type text as individual key presses, for the layout given.
pub fn type_keys(sequences: &[Vec<u16>], delay_ms: u64) -> Result<()> {
    for chord in sequences {
        press_keys(chord)?;
        if delay_ms > 0 {
            std::thread::sleep(std::time::Duration::from_millis(delay_ms));
        }
    }
    Ok(())
}
