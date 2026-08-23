# linux-gui-mcp — Rust

Drive a KDE/Wayland desktop over MCP. One binary, talking to the kernel and to
D-Bus directly.

**This branch is a work in progress and does not run yet.** The working server
is on the `python` branch; see `NOTES-rust-port.md` for exactly what is done
and what is not.

## Why one binary

KWin grants `ScreenShot2` by looking up the calling executable's desktop file.
On the `python` branch that forced a separate capture helper: granting the
interface to a Python interpreter would have handed screen capture to every
Python program on the machine. Here there is a single executable, so there is a
single grant.

```
cargo build --release
cp linux-gui-mcp.desktop ~/.local/share/applications/
```

Capture then costs about 40ms for a window, against spectacle's 560ms.

## Why native rather than shelling out

- **Input** goes straight to `uinput`. No ydotool, and no daemon to keep
  running or explain when it is missing.
- **Capture** asks KWin for the pixels over D-Bus, which skips starting a Qt
  application.
- **Tray, windows, clipboard** are D-Bus and Wayland protocols, spoken
  directly rather than through `busctl`, `kdotool` and `wl-copy`.

## Layout

| Module | Talks to |
| --- | --- |
| `shell.rs` | subprocesses, and the session environment they need |
| `input.rs` | the kernel, through uinput |
| `capture.rs` | KWin, over D-Bus |
