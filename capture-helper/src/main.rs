//! Capture the screen through KWin, without starting a Qt application.
//!
//! `spectacle` costs about a second, almost all of it Qt starting up. KWin
//! will hand the pixels straight over D-Bus instead - it writes them into a
//! file descriptor we pass it - but only to a process it recognises. That
//! recognition is by executable, so this is an executable that does nothing
//! else.
//!
//! Usage: kwin-capture <screen|window|area> <out.png> [x y w h]

use std::collections::HashMap;
use std::io::Read;
use std::os::fd::{AsFd, OwnedFd};

use zbus::blocking::Connection;
use zbus::zvariant::{Fd, OwnedValue, Value};

const SERVICE: &str = "org.kde.KWin";
const PATH: &str = "/org/kde/KWin/ScreenShot2";
const INTERFACE: &str = "org.kde.KWin.ScreenShot2";

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: kwin-capture <screen|window|area> <out.png> [x y w h]");
        std::process::exit(2);
    }
    let what = args[1].as_str();
    let out = &args[2];

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
            if args.len() < 7 {
                eprintln!("area needs x y w h");
                std::process::exit(2);
            }
            let numbers: Vec<i32> = args[3..7].iter().filter_map(|a| a.parse().ok()).collect();
            if numbers.len() != 4 {
                eprintln!("area needs four numbers");
                std::process::exit(2);
            }
            connection.call_method(
                Some(SERVICE),
                PATH,
                Some(INTERFACE),
                "CaptureArea",
                &(
                    numbers[0],
                    numbers[1],
                    numbers[2] as u32,
                    numbers[3] as u32,
                    options,
                    Fd::from(write_end.as_fd()),
                ),
            )
        }
        other => {
            eprintln!("unknown target {other:?}; use screen, window or area");
            std::process::exit(2);
        }
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
        .ok_or("the pixels did not fit the reported size")?
        .save(out)?;
    Ok(())
}

fn number(metadata: &HashMap<String, OwnedValue>, key: &str) -> Result<u64, String> {
    let value = metadata.get(key).ok_or(format!("no {key} in the reply"))?;
    u32::try_from(value)
        .map(u64::from)
        .or_else(|_| i32::try_from(value).map(|n| n as u64))
        .map_err(|_| format!("{key} was not a number"))
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
