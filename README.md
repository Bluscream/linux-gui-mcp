# linux-gui-mcp

Drive a KDE/Wayland desktop over MCP: find and focus windows, click, drag,
type, press shortcuts — and see the result.

## Why it is built this way

**Every action returns a screenshot.** An agent cannot see a screen, so an
action whose only answer is `ok` leaves it guessing whether the click landed
on the button or on the empty space beside it. Returning the picture makes the
next decision based on what happened rather than on what was meant to happen.

**Every action takes `settle_ms`.** Input is delivered the instant it is sent,
but nothing has redrawn yet. A screenshot taken immediately shows the state
*before* the action, which reads exactly like the action having failed.

**Three programs, because on Wayland no one of them is enough:**

| Job | Tool | Why that one |
| --- | --- | --- |
| Input | `ydotool` | Writes to the kernel's `uinput`, so it reaches native Wayland windows. `xdotool` only reaches XWayland ones. |
| Windows | `kdotool` | Drives KWin's own scripting interface; nothing else can enumerate Wayland windows. |
| Pictures | `spectacle` | KDE's screenshot tool, and the only one here that can capture a single window correctly. |

## Requirements

Everything below is already present on Bazzite/Kinoite with KDE 6:

- `ydotool` and `ydotoold` — the daemon is started automatically if missing
- `kdotool`
- `spectacle`
- `/dev/uinput` writable by your user (an ACL is enough; no root needed)

## Tools

| Tool | Notes |
| --- | --- |
| `find_window(pattern=".")` | every open window: id, title, class, pid, geometry |
| `active_window()` | which window has focus |
| `screenshot(id?)` | the screen, or one window |
| `interact(...)` | click, drag, scroll, paste, type, press keys - in that order, one call |
| `input_settings()` | which layout, whether pasting is available |
| `wait_for_window(pattern)` | waits, then shows it |
| `wait_for_process(pattern)` | pid, plus any windows it owns |
| `run_app(executable, args?)` | starts a program, or focuses one already running |
| `run_in_terminal(command)` | for anything needing a tty |
| `list_tray_items()` | id, title, status, owning pid |
| `tray_action(pattern, action)` | activate, secondary, context, or scroll |
| `get_process_info(target)` | a process and everything it put on screen |

## interact

One tool rather than one per gesture. Everything is optional and happens in a
fixed order - pointer, scroll, paste, type, keys - so a single call can click
a field and type into it, which is the usual shape of a UI step and would
otherwise be two round trips each paying for a screenshot.

Coordinates are **window-relative whenever a `window_id` is given**, and they
match the screenshot that comes back: the window is captured by its frame
rectangle, so image (0,0) is the same point as window-relative (0,0).
Capturing "the active window" instead returns the client area, which excludes
the titlebar - and then every coordinate read off a screenshot is about 28
pixels too high, which looks like the click having missed.

`paste_text` and `type_text` are different on purpose. Pasting goes through
the clipboard and no keyboard layout can garble it, but some fields refuse a
paste and an application watching for key events sees none. Typing sends real
keystrokes, rewritten for the layout, because key codes name positions on a
keyboard rather than letters.

## The tray

Read and driven over D-Bus (`StatusNotifierItem`), not by clicking pixels.
The tray is a row of identical little squares, so recognising one by sight is
guesswork - and only the panel knows where each sits, so an item that moved
would take the click somewhere else. Every item publishes its own id, title
and status, which is the difference between clicking "the third icon" and
clicking Steam.

A pattern that matches two items is refused rather than resolved to the first:
that is a question the caller has to answer.

## Running things

`run_app` starts a GUI program; `run_in_terminal` runs anything that needs a
tty to exist at all - a TUI, an interactive prompt. The terminal is held open
after the command finishes so its last output is still readable.

The session environment is passed explicitly rather than inherited. A program
started without `WAYLAND_DISPLAY` does not fail loudly; it simply never
appears, which is a bad half hour to spend.

A process that exits before showing a window is reported as having failed to
start, rather than timing out silently after 20 seconds.

**This is arbitrary command execution in your desktop session.** That is the
point of it, and worth knowing when deciding which clients to register it in.

## Errors

A window that has closed is the ordinary case, not an exotic one, so it is
checked before every action and reported with what to do next:

```
no window '{4f2a…}' is open. Currently open: konsole:'blu : bash', firefox:'…'.
Call find_window or list_windows for ids that are still valid.
```

Existence is judged on whether the geometry parses, not on the exit status —
`kdotool` answers a made-up window id with a success and an empty line, so
trusting the status would report every closed window as open.

## Registering it

```json
{
  "mcpServers": {
    "linux-gui": {
      "command": "/run/media/system/Data/Projects/MCPs/linux-gui-mcp/.venv/bin/python",
      "args": ["/run/media/system/Data/Projects/MCPs/linux-gui-mcp/server.py"]
    }
  }
}
```

## Typing and pointing, on a real desktop

Two things that look like they should just work, and do not:

**Text is pasted by default, not typed.** ydotool speaks raw kernel keycodes -
positions on the keyboard, not letters. On a German layout that turns "typed"
into "tzped" and ":" into "Oe". Pasting is layout-proof and faster, and the
previous clipboard contents are put back afterwards.

Pasting is not always right: some fields refuse a paste, and an application
watching for key events sees none. `method="keystrokes"` sends real key events
and rewrites the text first for `layout` (defaulting to the session's own), so
that pressing US positions produces the characters asked for. Those tables are
partial by design - they cover the letter swaps and the punctuation that
actually moves, and anything unmapped is sent unchanged, because wrong is
recoverable and missing is not.

`input_settings()` reports which layout is in use, whether pasting is
available, and which layouts the keystroke path can rewrite for.

**Pointer moves are calibrated at startup.** ydotool's `--absolute` maps onto
the virtual device's coordinate space and is affected by pointer acceleration
- its own help says "you need to disable mouse speed acceleration for correct
absolute movement". Rather than ask for that, `move_mouse` homes the pointer
into the corner and moves relatively, divided by a factor measured once by
moving a known distance and reading back where it landed. On this desktop the
factor is about 2.0; it depends on your acceleration settings, so it is
measured rather than assumed.

## Speed

Capturing is the expensive part - about a second, against tens of
milliseconds for everything else. So every action that returns a picture takes
`screenshot: bool`, and a sequence that only needs to see the end result can
decline the intermediate ones:

```
click without picture: 0.46s
click with picture:    1.69s
```

Capture goes through `capture-helper/`, a small Rust binary that asks KWin for
the pixels over D-Bus and writes them into a pipe. It exists as a separate
executable for one reason: KWin grants `ScreenShot2` by looking up the calling
executable's desktop file, so the permission follows the binary. Granting it
to a Python interpreter would hand screen capture to every Python program on
the machine; granting it to this hands it to one program that captures the
screen and does nothing else.

Build it and install the grant:

```
cd capture-helper && cargo build --release
cp kwin-capture.desktop ~/.local/share/applications/
```

Without it everything still works - it falls back to spectacle, which is
always allowed and slower. With it:

| | spectacle | kwin-capture |
| --- | --- | --- |
| whole screen | 0.56s | 0.45s |
| one window | 0.56s | **0.04s** |

The window case is the one that matters, since most actions capture a window.

Two other routes were tried and are dead ends. `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1`
on the compositor works but disables the check for everything on the machine.
Spectacle's own D-Bus methods are `no-reply` and return neither the image nor
its path, so using them would mean rewriting the save-location config and
polling a guessed path.

Reading the tray was 0.72s because each item's five properties were fetched
separately. `busctl get-property` takes several names at once, which brought
it to 0.34s. Not `GetAll`, which also returns the icon pixmap - several
thousand bytes of image per item, none of it wanted.

## Known gaps

**Finding a control by its label** is not possible here. It needs an
accessibility tree, and KDE's Qt applications do not publish one - the AT-SPI
bus lists only tray helpers and GTK programs, so `kwrite` is invisible to it
while its window is plainly on screen. Screenshot, read the position off it,
and pass coordinates. Enabling it would mean `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`
session-wide and restarting every application.

**Multi-monitor origins.** Clicking uses compositor coordinates and may need
an offset on a secondary output. Window-relative coordinates are unaffected.
