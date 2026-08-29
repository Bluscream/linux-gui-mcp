# linux-gui-mcp

Drive a KDE/Wayland desktop over MCP with a consolidated high-leverage toolset: window management, closed-loop subpixel pointer targeting, OCR text search & clicking, D-Bus system tray control, and visual screenshots.

## Architecture & Principles

- **Every action returns screenshots & structured JSON metadata**: Agents see exactly what changed and where pointer coordinates landed.
- **Closed-loop pointer calibration**: Compensates for Wayland pointer acceleration and displays subpixel-accurate mouse targeting.
- **OCR text search & clicking**: Uses Tesseract OCR to automatically find and click text anywhere on screen or inside a specific window.
- **Dynamic safety controls**: Re-evaluates `BLOCK_INTERACT` environment variable on every tool call.

## Environment Variables

- `BLOCK_INTERACT` (`1` / `true` / `on` / `yes` / `enabled`): When set, re-evaluated dynamically on every call to block all mutating / interactive desktop actions (`interact`, `run_app`, `tray`) while leaving read-only tools (`find_window`, `screenshot`, `get_process_info`) functional.
- `LINUX_GUI_MCP_SHOTS`: Custom directory for storing screenshots (defaults to `/tmp/linux-gui-mcp`).

## Consolidated Tools (6 High-Leverage Tools)

| Tool | Description |
| :--- | :--- |
| **`find_window`** | Unified window query: lists open windows, gets focused window (`focused_only=True`), or waits for windows (`wait_timeout_s > 0`). |
| **`screenshot`** | Captures the entire desktop or a specific window by ID / title / class pattern. |
| **`interact`** | Complete all-in-one GUI interaction in a single call: window focus, move/resize, OCR text search (`text="..."`), click/drag, scroll, type/paste, shortcut keys, and pointer position tracking. |
| **`run_app`** | Launches GUI applications or runs interactive terminal commands (`terminal=True`). |
| **`tray`** | Unified system tray tool: enumerates tray icons (`action="list"`) or interacts with them (`activate`, `context`, `secondary`, `scroll`) over D-Bus. |
| **`get_process_info`** | Deep process introspection: inspects PID, command line args, credentials-blanked environment, open windows, and owned tray icons (with optional `wait_timeout_s`). |

---

### `interact` Workflow Order in a Single Call

1. **Focus/Raise Window** (if `window_id` provided)
2. **Move/Resize Window** (if `move_window_x`, `move_window_y`, `resize_width`, `resize_height` provided)
3. **Capture Initial Pointer Position** (reported in metadata)
4. **OCR Text Targeting** (if `text` provided, locates text bounding box & calculates center)
5. **Mouse Action** (move, click, double-click, drag)
6. **Scroll Wheel** (if `scroll_amount` provided)
7. **Keyboard Input** (`paste_text`, `type_text`, `keys`)
8. **Capture Final Pointer Position** (reported in metadata)
9. **Capture Screenshot** (returned as standard MCP `ImageContent`)
