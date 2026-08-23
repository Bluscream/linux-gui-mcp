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
| `list_windows` | id, title, class, pid, geometry |
| `find_window(pattern)` | regex against title and class |
| `focus_window(id)` | raises it, returns geometry and a picture |
| `screenshot(id?)` | whole screen, or one window |
| `click(x, y, id?, button, count)` | coordinates are window-relative when an id is given |
| `drag(from, to, id?)` | moves in steps, so drop targets see the pointer arrive |
| `type_text(text, id?, method?, layout?)` | pastes by default; `keystrokes` for real key events |
| `input_settings()` | which layout, whether pasting is available, what the alternatives are |
| `press_keys("ctrl+shift+k", id?)` | |
| `scroll(amount, x?, y?, id?)` | |
| `wait_for_window(pattern, timeout_s, settle_ms)` | |
| `wait_for_process(pattern, ...)` | returns the pid and any windows it owns |
| `active_window()` | |
| `run_app(command, cwd?, wait_for_window_s)` | starts a GUI program detached in the session |
| `run_in_terminal(command, cwd?)` | runs it in a real konsole window, held open afterwards |
| `list_tray_items()` | id, title, status and icon for everything in the tray |
| `click_tray_item(pattern, action)` | `activate` (left), `secondary` (middle), `context` (right) |
| `scroll_tray_item(pattern, delta)` | volume applets use this |

Coordinates are **window-relative whenever a `window_id` is given**. A tool
that only spoke screen coordinates would send every click somewhere
unintended the moment a window moved, and would do it silently.

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

Three faster capture paths were tried and none of them work here:

- **`org.kde.KWin.ScreenShot2`** takes a file descriptor and would skip Qt
  startup entirely. KWin refuses: *"The process is not authorized to take a
  screenshot"* - only whitelisted binaries may call it. That is a deliberate
  KDE restriction, not a missing dependency.
- **`org.kde.Spectacle`** over D-Bus keeps one process warm, but its methods
  are `no-reply` and return neither the image nor its path. Using it would
  mean rewriting the user's save-location config and polling a guessed path.
- **Warm cache** makes no difference: the second run costs the same as the
  first, and 0.93s of the 1.02s is CPU. It is Qt starting up, not I/O.

Reading the tray was 0.72s because each item's five properties were fetched
separately. `busctl get-property` takes several names at once, which brought
it to 0.34s. Not `GetAll`, which also returns the icon pixmap - several
thousand bytes of image per item, none of it wanted.

## Known gaps

**Multi-monitor origins.** With more than one output, a full screenshot and
the compositor do not share an origin, which is why window capture asks KDE
for the active window instead of cropping a full capture to a window's
geometry. Clicking still uses compositor coordinates and may need an offset on
the secondary output.
