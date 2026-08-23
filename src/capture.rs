//! Capture the screen through KWin, without starting a Qt application.
//!
//! `spectacle` costs about half a second, almost all of it Qt starting up.
//! KWin will hand the pixels straight over D-Bus instead - it writes them into
//! a file descriptor we pass it - but only to a process it recognises, and it
//! recognises them by executable.
//!
//! That is why this being one binary matters. On the Python branch the capture
//! had to be a separate executable so the permission could be granted to it
//! alone; granting it to a Python interpreter would have handed screen capture
//! to every Python program on the machine. Here there is one binary to grant
//! it to, which is the whole thing this server is.

use anyhow::{anyhow, Result};

use std::collections::HashMap;
use std::io::Read;
use std::os::fd::{AsFd, OwnedFd};

use zbus::blocking::Connection;
use zbus::zvariant::{Fd, OwnedValue, Value};

const SERVICE: &str = "org.kde.KWin";
const PATH: &str = "/org/kde/KWin/ScreenShot2";
const INTERFACE: &str = "org.kde.KWin.ScreenShot2";

pub fn capture(what: &str, out: &str, area: Option<(i32, i32, i32, i32)>) -> Result<()> {

    let (read_end, write_end) = pipe()?;
    let connection = Connection::session()?;
    let options: HashMap<&str, Value> = HashMap::from([("include-cursor", Value::from(false))]);

    // Sent before the pixels are read: KWin writes into the pipe and only then
    // answers, so reading first would wait for a reply that is waiting for us.
    let reply = match what {
        "screen" => connection.call_method(
            Some(SERVICE),
            PATH,
            Some(INTERFACE),
            "CaptureActiveScreen",
            &(options, Fd::from(write_end.as_fd())),
        ),
        "window" => connection.call_method(
            Some(SERVICE),
            PATH,
            Some(INTERFACE),
            "CaptureActiveWindow",
            &(options, Fd::from(write_end.as_fd())),
        ),
        "area" => {
            let (x, y, w, h) = area.ok_or_else(|| anyhow!("area needs x, y, width and height"))?;
            connection.call_method(
                Some(SERVICE),
                PATH,
                Some(INTERFACE),
                "CaptureArea",
                &(x, y, w as u32, h as u32, options, Fd::from(write_end.as_fd())),
            )
        }
        other => anyhow::bail!("unknown target {other:?}; use screen, window or area"),
    }?;

    // Dropped so the read end sees end-of-file once KWin is finished.
    drop(write_end);

    let metadata: HashMap<String, OwnedValue> = reply.body().deserialize()?;
    let width = number(&metadata, "width")? as u32;
    let height = number(&metadata, "height")? as u32;
    let stride = number(&metadata, "stride")? as usize;

    let mut raw = Vec::with_capacity(stride * height as usize);
    std::fs::File::from(read_end).read_to_end(&mut raw)?;

    // KWin hands over premultiplied BGRA, one row every `stride` bytes -
    // which is not always width * 4, so rows are copied rather than the
    // buffer being reinterpreted.
    let mut rgba = Vec::with_capacity((width * height * 4) as usize);
    for row in 0..height as usize {
        let start = row * stride;
        for pixel in raw[start..start + width as usize * 4].chunks_exact(4) {
            rgba.extend_from_slice(&[pixel[2], pixel[1], pixel[0], pixel[3]]);
        }
    }

    image::RgbaImage::from_raw(width, height, rgba)
        .ok_or_else(|| anyhow!("the pixels did not fit the reported size"))?
        .save(out)?;
    Ok(())
}

fn number(metadata: &HashMap<String, OwnedValue>, key: &str) -> Result<u64> {
    let value = metadata.get(key).ok_or_else(|| anyhow!("no {key} in the reply"))?;
    u32::try_from(value)
        .map(u64::from)
        .or_else(|_| i32::try_from(value).map(|n| n as u64))
        .map_err(|_| anyhow!("{key} was not a number"))
}

fn pipe() -> std::io::Result<(OwnedFd, OwnedFd)> {
    let mut ends = [0; 2];
    // Safe: the array is the size libc expects and is written before use.
    let made = unsafe { libc_pipe(ends.as_mut_ptr()) };
    if made != 0 {
        return Err(std::io::Error::last_os_error());
    }
    unsafe {
        use std::os::fd::FromRawFd;
        Ok((OwnedFd::from_raw_fd(ends[0]), OwnedFd::from_raw_fd(ends[1])))
    }
}

unsafe extern "C" {
    #[link_name = "pipe"]
    fn libc_pipe(fds: *mut i32) -> i32;
}
