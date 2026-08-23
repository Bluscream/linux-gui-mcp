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
    AbsInfo, AbsoluteAxisCode, AbsoluteAxisEvent, AttributeSet, EventType, InputEvent, KeyCode,
    KeyEvent, PropType, RelativeAxisCode, UinputAbsSetup,
};
use std::sync::{Mutex, OnceLock};

/// The coordinate space the virtual pointer reports in.
///
/// Fixed and large rather than matched to the screen: the compositor scales
/// whatever range the device declares onto the desktop, so a constant here
/// keeps this independent of how many monitors are plugged in or how they are
/// arranged.
const ABS_MAX: i32 = 65535;

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
    let axis = AbsInfo::new(0, 0, ABS_MAX, 0, 0, 1);
    let pointer = VirtualDeviceBuilder::new()?
        .name("linux-gui-mcp pointer")
        .with_keys(&buttons)?
        .with_absolute_axis(&UinputAbsSetup::new(AbsoluteAxisCode::ABS_X, axis))?
        .with_absolute_axis(&UinputAbsSetup::new(AbsoluteAxisCode::ABS_Y, axis))?
        // Absolute axes only. A device carrying both absolute and relative
        // axes is ambiguous, and libinput classifies it as something that is
        // not a pointer - so the wheel lives on its own device below.
        .with_properties(&AttributeSet::from_iter([PropType::POINTER]))?
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
    std::thread::sleep(std::time::Duration::from_millis(300));
    Ok(Devices { pointer, wheel, keyboard })
}

/// Put the pointer somewhere, in desktop coordinates.
pub fn move_mouse(x: i32, y: i32, width: i32, height: i32) -> Result<()> {
    let scaled_x = scale(x, width);
    let scaled_y = scale(y, height);
    let mut held = devices()?.lock().unwrap();
    held.pointer.emit(&[
        *AbsoluteAxisEvent::new(AbsoluteAxisCode::ABS_X, scaled_x),
        *AbsoluteAxisEvent::new(AbsoluteAxisCode::ABS_Y, scaled_y),
    ])?;
    Ok(())
}

fn scale(value: i32, span: i32) -> i32 {
    if span <= 0 {
        return 0;
    }
    let clamped = value.clamp(0, span);
    ((clamped as i64 * ABS_MAX as i64) / span as i64) as i32
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
