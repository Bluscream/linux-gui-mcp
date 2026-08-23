# Rust port — state of play

`python` branch holds the working server, and is what should be registered.
`main` carries both, so the Python server keeps working whichever branch is
checked out. **The Rust port does not drive the pointer yet.**

## Working

- `shell.rs` — session environment and subprocess helpers. The environment
  matters more than it looks: the server is started by an MCP client with
  almost nothing set, and every desktop tool needs `DBUS_SESSION_BUS_ADDRESS`
  and `WAYLAND_DISPLAY` to find the session.
- `input.rs` — creates virtual keyboard, pointer and wheel devices through
  `uinput`. The kernel registers them and the pointer gets a `mouse` handler,
  which is the right classification. No ydotool, and no daemon.
- Screen capture is already proved in Rust, on the `python` branch, in
  `capture-helper/`: KWin hands the pixels over D-Bus in 40ms against
  spectacle's 560ms.

## The open problem

`move_by(100, 100)` visibly moves the cursor. `move_to()` does not move it at
all, and neither do the 40 small steps inside `home()`. Both go through the
same `move_by`, in the same process, on a device the kernel has registered and
given a `mouse` handler.

That contradiction is the thing to chase, and it has not been explained. Do
not assume the earlier theories - they were tested and none held:

- **Not the device classification.** A device with both absolute and relative
  axes is classified as not-a-pointer, which is real and worth keeping (the
  wheel is on its own device because of it). But the pointer now presents as a
  plain relative mouse and gets `Handlers=mouse5 event31`.
- **Not `INPUT_PROP_POINTER`.** It does not make libinput read absolute axes
  as positions.
- **Not the tablet route.** `BTN_TOOL_PEN` + `BTN_TOUCH` stopped movement
  entirely; a real tablet needs the pressure and tool protocol.
- **Not the settle after creation.** Raised from 300ms to a full second, no
  change.
- **Probably not large-delta rejection.** `home()` was changed from one
  -20000 jump to 40 steps of -250, no change.

The next thing to establish is whether `kdotool getmouselocation` is even
telling the truth here - every failing run reported the *same* coordinates,
which is as consistent with a stale reading as with a motionless cursor.
Confirm the cursor position by screenshotting with the cursor included, rather
than by asking kdotool, before theorising further about uinput.

## Still to port

Windows (KWin scripting over D-Bus, currently `kdotool`), the tray (`zbus`
rather than `busctl`), the clipboard (`wl-clipboard-rs` rather than `wl-copy`),
processes (`/proc`, straightforward), and the MCP server itself (`rmcp` 3).

## If uinput proves to be a dead end

The Python version's approach is unlovely and accurate to a pixel: home the
pointer into a corner, then move relatively, divided by a scale factor
measured once at startup by moving a known distance and reading back where it
landed.
