# Rust port — state of play

`python` branch holds the working server. `main` is the port. **The port does
not work yet** and the Python one is what should be registered meanwhile.

## Done

- `shell.rs` — session environment and subprocess helpers. The environment
  matters more than it looks: the server is started by an MCP client with
  almost nothing set, and every desktop tool needs `DBUS_SESSION_BUS_ADDRESS`
  and `WAYLAND_DISPLAY` to find the session.
- `input.rs` — creates virtual keyboard, pointer and wheel devices through
  `uinput` and writes events to them. No ydotool, and no daemon to keep
  running.
- Screen capture already proved out in Rust on the `python` branch: KWin hands
  the pixels over D-Bus in 40ms against spectacle's 560ms.

## The open problem: absolute pointing

The pointer device declares `ABS_X`/`ABS_Y` and `INPUT_PROP_POINTER`. The
kernel registers it and the compositor moves the cursor when it is written to,
but it treats the coordinates as *relative deltas* rather than positions - so
asking for (200,200) then (1500,700) drifts the cursor a little each time
instead of placing it.

Two findings on the way there, both worth keeping:

- A device carrying both absolute and relative axes is ambiguous and gets
  classified as something that is not a pointer at all. Splitting the wheel
  onto its own device is what made the cursor move at all.
- `INPUT_PROP_POINTER` alone is not enough for libinput to read the axes as
  absolute.

The likely answer is that libinput only reads absolute axes as positions for
devices that look like a touchscreen (`INPUT_PROP_DIRECT` plus `BTN_TOUCH`) or
a tablet (`BTN_TOOL_PEN`), rather than a mouse. Worth trying in that order.

If none of that works, the fallback is the approach the Python version uses:
relative movement, homed into a corner first, divided by a scale factor
measured once at startup. It is unlovely and it is accurate to a pixel.

## Still to port

Windows (KWin scripting over D-Bus, currently `kdotool`), the tray (`zbus`
rather than `busctl`), the clipboard (`wl-clipboard-rs` rather than `wl-copy`),
processes (`/proc`, straightforward), and the MCP server itself (`rmcp` 3).
