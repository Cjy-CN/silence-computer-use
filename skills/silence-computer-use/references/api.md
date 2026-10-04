# KimiCU API reference

KimiCU exposes one method set with two entry points:

| Entry point | Drives | How you reach it |
| --- | --- | --- |
| **Desktop CU** (`@kimi/cu`) | The user's own Windows session | `import("@kimi/cu")` inside the kimi-cu-win plugin's `js` tool |
| **silence-computer-use** (virtual desktop) | An isolated Windows shadow session — the user's screen is never touched | The `vd_cu.py` CLI (this package), or the named pipe `\\.\pipe\kimi-cu-win-vd` directly |

Both speak the **same methods with the same argument schemas**. Everything in
the *Method reference* section applies to both; only the connection differs.
The `@kimi/cu` side is JavaScript; the examples in this document use Node.js
syntax for it. The silence-computer-use side is driven from the shell through `vd_cu.py`
(Python, stdlib only).

All methods are asynchronous. Input methods resolve to `undefined`, not a
`clicked`/`success` object or application-state proof; use `await`, then
observe when verification is needed. Parameter names and scroll signs differ
from the legacy MCP tools.

## Connecting: desktop CU (`@kimi/cu`)

Inside the kimi-cu-win plugin's `js` tool, top-level `await` is available:

```js
const { kimiCU } = await import("@kimi/cu");
const windows = await kimiCU.list_windows();
console.log(windows);
```

The `js` tool accepts an optional `title`: a short user-facing description
(1–80 characters, not whitespace-only), alongside `code` and `timeout_ms`.
It is for host display only, not JavaScript or proof that an action succeeded;
omitting it does not change execution. Rendering depends on the host.

## Connecting: silence-computer-use (virtual desktop)

The vd agent runs **inside** the shadow session (real SendInput input only
works session-locally, so the agent must live there) and listens on its own
pipe, `\\.\pipe\kimi-cu-win-vd`. Never touch `\\.\pipe\kimi-cu-win-<username>`
— that is the user-session agent the kimi-cu-win plugin owns.

### Session lifecycle (CLI)

The `vd_cu.py` CLI (Python ≥ 3.10, stdlib only) covers the whole lifecycle:

```powershell
python scripts/vd_cu.py doctor   # new machine/host: dependency checks, all green before continuing
python scripts/vd_cu.py up       # shadow session + in-session agent (empty-password accounts can enter without a card)
python scripts/vd_cu.py status   # session + agent health
python scripts/vd_cu.py down     # teardown: destroys the whole shadow session, agent included
```

It also exposes `call` (one method call), `tree` (print a window's UIA tree,
optionally keyword-filtered) and `serve` (an NDJSON stdin/stdout loop holding
one lease — one JSON line in, one line out,
`{"id":N,"method":"...","arguments":{...}}`, optional per-request
`"tree_filter":"keyword"`).

`up` checks an empty local credential before deciding whether to show a card.
An accepted empty credential or account-restriction result follows a card-free
SDK attempt; only actual rendering proves startup. A required password or
inconclusive detection uses a local card on the user's screen (not visible to
the virtual-session agent). Warn the user first; failure stops without retries.
Only one shadow session can exist at a time; reuse it if already up.

### The vd pipe protocol

`scripts/cu_bridge.py` is the reference implementation for everything in
this section — read it before reimplementing the protocol anywhere else. The
wire format (measured from the kimi-cu-win trusted-service sources):

- Named pipe, 8-byte little-endian uint64 length prefix + UTF-8 JSON payload.
- One request, one response per connection. The lifecycle lease is the one
  persistent connection; calls carry its nonce.

Request shapes:

```jsonc
{"id": 1, "session_id": "...", "method": "__agent_ping", "params": {}}
{"id": 2, "session_id": "...", "method": "__lifecycle_v1",
 "params": {"action": "begin", "nonce": "..."}}            // or "end"
{"id": 3, "session_id": "...", "method": "__computer_use_call_v1",
 "params": {"method": "click", "arguments": {...}},
 "telemetry_tool": "click", "lifecycle": {"nonce": "..."}}
```

Two vd-side conveniences the raw pipe does not give you (both implemented by
`vd_cu.py call`/`serve`):

- **Screenshots** come back as data URLs in `screenshots[].url`. The CLI
  decodes and saves them to `./cu_shots/`, replacing the URL with a file path.
- **Huge UIA trees** can swamp a chat UI or pipe consumer. The `serve` loop
  truncates long trees inline (full text to `./cu_trees/tree_*.txt`) and
  accepts `"tree_filter"` to keep only matching lines inline. Over the raw
  pipe, filter `accessibility.tree.split("\n")` yourself.

### silence-computer-use hard rules

1. Windows listed as `KimiCU Overlay` / `KimiCU Display Overlay` belong to the
   agent itself — **never click them**.
2. Packaged apps may fail `launch_app` ("access denied" on app execution
   aliases); launch them through the engine instead:
   `python runtime/vdcu/run_cli.py launch "记事本"`. Cold starts can take
   ~20 s before the window appears — poll `list_windows`.
3. KimiCU captures single windows only; for a full-desktop frame use
   `run_cli.py capture --shot look.jpg`.
4. The shadow session shares the filesystem and clipboard with the user —
   write outputs to user-visible paths, never paste secrets into it.
5. Finish with `down`; clean up strays with `run_cli.py cleanup`.

## Method reference

### Windows and observation

| Method | Arguments / result |
| --- | --- |
| `list_apps()` | Returns `Array<{displayName: string, id: string, isRunning: boolean, windows: Window[]}>` for applications with enumerated windows; not an installed-app inventory. |
| `list_available_apps()` | No arguments. Returns `Array<{displayName: string, appId: string}>` from Windows StartApps, including apps without windows. Read-only; does not launch or activate anything. |
| `list_windows()` | Returns windows with `{app, id, title}`. Keep the returned object. |
| `get_window({id, app?})` | Resolve an observed numeric HWND to a window. |
| `launch_app({app})` | Known executable path/resolvable name, exact StartApps display name or returned `appId`. Returns no window; list windows afterward. |
| `activate_window({window})` | Bring an existing window forward; useful for minimized windows. |
| `get_window_state({window, include_screenshot?, include_text?})` | Returns `WindowState` below, without activation. Both flags default to `true` in KimiCU; set them explicitly to avoid unnecessary work. Both `false` is invalid. |

#### Optional app discovery

`launch_app` does not require a prior catalog query. If a usable display name,
AppID or executable path is known, launch directly. When the identifier is
uncertain or name resolution fails, this optional query can help find it:

```js
const availableApps = await kimiCU.list_available_apps();
const appMatches = availableApps.filter(a => /酷狗|kugou/i.test(a.displayName + " " + a.appId));
console.log(appMatches);
```

Replace the search terms with the requested app. Inspect matches and choose the
intended entry; if several remain plausible, resolve the ambiguity rather than
silently taking the first. Pass that entry's `appId` as `launch_app`'s **`app`**
argument, then list windows and observe the newly available target:

```js
await kimiCU.launch_app({app: selectedApp.appId}); // selectedApp is a returned entry.
console.log(await kimiCU.list_windows());
```

`appId` is an opaque Windows launch identifier, not a path or window ID. This
catalog shares the exact StartApps resolver used by `launch_app`; display names
can be duplicated, so prefer the returned ID. The catalog is not a complete
software inventory: portable/unregistered programs may be absent. No matches
does not prove an app is uninstalled; use a known executable path or ask for it.
A bare EXE name only works when directly resolvable (for example on PATH), not
merely because that file exists somewhere on disk. Enumeration errors are
reported as errors, not an empty successful list. Launch dispatch still does
not guarantee that an app is ready or that a window will appear immediately.

### Returned state

These types describe results, not extra APIs to import. A `?` field may be
absent; absence does not prove that the app has no focus/selection/document.

```ts
type Window = {app: string; id: number; title: string};
type AccessibilityState = {
  tree: string;                 // Formatted lines containing original [element_index] labels.
  focused_element?: string;    // Formatted "Focused:" line, not a nested node or an index.
  selected_elements?: string[]; // Formatted selected-element lines, not element objects.
  selected_text?: string;      // Reserved optional field; currently not populated by Windows.
  document_text?: string;      // Document text when the provider exposes it.
};
type Screenshot = {
  id: string; width: number; height: number; zIndex: number;
  url: string; // In-plugin: MCP image placeholder. vd pipe: data URL (vd_cu.py saves a file path instead).
};
type WindowState = {
  window: Window;
  accessibility: AccessibilityState | null;
  screenshots: Screenshot[];   // [] when include_screenshot is false.
  coordinate_space: {
    kind: "screenshot_pixels"; origin: "top_left";
    image_width: number; image_height: number;
    window_rect: {left: number; top: number; right: number; bottom: number;
      width: number; height: number};
    input_revision: number;    // Input tick metadata, not a UIA/screenshot consistency proof.
  };
  accessibility_status?: {status: "timeout"; reason: "uia_provider_timeout";
    timeout_ms: number; retryable: boolean; next_step: string};
};
```

There are no `state.windows` or `state.summary` fields in code mode. List windows
separately for dialogs. `accessibility` is `null` when text was not requested or
the UIA observation timed out; a timeout also supplies `accessibility_status`.
On normal success that status is absent, not `{status: "success"}`.

The `tree` is **one string**, not DOM nodes or a `children` hierarchy. Read it
directly, or use `.split("\n")` for filtering. Do not stringify the accessibility
object with `String(...)`: that produces `[object Object]`. `document_text` is
optional document content, not a replacement for the indexed tree.

### Observe and read

After `list_windows()` returned the desired window, keep that actual object as
`observedWindow`. If several candidates match, inspect their titles/IDs to select
the intended one rather than silently taking the first.

```js
const state = await kimiCU.get_window_state({
  window: observedWindow, include_screenshot: true, include_text: true,
});
console.log(state.accessibility?.tree ?? "No UIA text returned.");
console.log({
  focused_element: state.accessibility?.focused_element ?? null,
  selected_elements: state.accessibility?.selected_elements ?? [],
  accessibility_status: state.accessibility_status ?? null,
  screenshots: state.screenshots,
});
```

In the plugin's `js` tool the screenshot is displayed automatically when the
cell completes successfully; do not re-open or print image bytes. Over the vd
pipe, screenshots arrive as data URLs — save them and view the file. Text is
not displayed merely by returning/assigning `state`: use `console.log`. For
machine-readable JSON use `JSON.stringify` explicitly.

For only a screenshot, set `include_screenshot: true, include_text: false`.
For only UIA, set `include_screenshot: false, include_text: true`. A UIA timeout
does not invalidate a returned screenshot, but do not use old indexes until a
fresh UIA observation succeeds. Screenshot and UIA data are not guaranteed to
describe one atomic instant.

### Filter a retained observation

Filter the stored text without another runtime call. Replace these example
search terms with terms relevant to the task:

```js
const lines = (state.accessibility?.tree ?? "").split("\n");
const matches = lines.filter(line => /按钮|编辑|画笔|button|edit|brush/i.test(line));
console.log(matches.slice(0, 40).join("\n") || "No matching lines in this observation.");
```

Keep original lines and their `[index]` labels: the position in `matches` is
**not** `element_index`. Expand the filter/output when needed; no match does not
prove a control is absent. `focused_element` starts with `Focused:` and has no
actionable index; obtain an index from the tree, or use a grounded screenshot
coordinate. Filtering reuses a snapshot, not live state: refresh after UI changes.

## Targeting

Pass `window` returned by observation. Coordinates are **screenshot pixels**,
origin top-left of that window's screenshot, not desktop or CSS coordinates.
Pass `screenshotId: state.screenshots[0].id` with coordinate input. Preserve the
matching screenshot descriptor when fetching text only. Text-only observations
retain the last image's pixel scale while the window geometry is unchanged;
they return no new screenshot. Refresh after geometry
changes; do not rescale coordinates by guessing a DPI factor.

`click` accepts exactly one target shape:

```js
await kimiCU.click({window: state.window, element_index: observedIndex});
// OR: x/y chosen from the inspected screenshot.
await kimiCU.click({window: state.window,
  screenshotId: state.screenshots[0].id, x: observedX, y: observedY});
```

Optional click arguments: `mouse_button: "left" | "right" | "middle"`,
`click_count: 1 | 2 | 3`, and `timing` below. Index clicks use real mouse input,
not UIA Invoke. Never mix an index with x/y.

## Text and keys

`type_text({window, text})` pastes into **current focus**. It accepts no index,
coordinates, `clear` or `submit`. Click the intended input and confirm focus
when needed; use key presses for selection and submission. For example, once
the intended editable input is verified focused:

```js
await kimiCU.press_key({window: state.window, key: "Control_L+a"});
await kimiCU.type_text({window: state.window, text: "replacement text"});
// Observe the visible result before submitting, if submission is requested.
```

`press_key({window, key})` uses singular **`key`**, not legacy `keys`.
Supported forms include `Enter`, `Escape`, `Tab`, `Backspace`, `Delete`,
`Control_L+a`, `Control_L+v`, `Shift_L+Tab`, `F5`, `PageDown`, `Insert`, and
`Left/Right/Up/Down` (also `ArrowLeft/ArrowRight/ArrowUp/ArrowDown`).
Named punctuation: `equal`, `minus`, `comma`, `period`, `semicolon`, `slash`,
`backslash`, `grave`, `bracketleft`, `bracketright`, `apostrophe`.
These are physical keyboard keys; Shift is not implicit. `Shift_L+equal` is `+`
on a US keyboard; `plus`/`KP_Add` is keypad plus. A literal `+` separates a chord.

Text remains on the clipboard. A resolved paste does not guarantee matching
text, correct focus or successful submission; inspect before retrying.

## Scroll, drag and native controls

| Method | Arguments |
| --- | --- |
| `scroll` | `{window, screenshotId?, x, y, scrollX, scrollY}` |
| `drag` | `{window, screenshotId?, from_x, from_y, to_x, to_y, timing?}` |
| `drag_path` | `{window, screenshotId?, points: [[x,y], ...], timing?}` |
| `set_value` | `{window, element_index, value}`; `value` is a string. |
| `perform_secondary_action` | `{window, element_index, action}` |

Scroll at the observed x/y location. **`scrollY > 0` goes down, `< 0` goes up;
`scrollX > 0` goes right.** Supply both scroll components (zero for the unused
axis). Values are runtime wheel units, not a promise of exact content pixels.
Do not copy the old MCP `dy` sign, which was the opposite.

`drag` is one straight drag with ten interpolated moves; there is no `steps`
argument in this facade. `drag_path` is one continuous left-button gesture,
not a batch of gestures. It accepts 2–1024 integer point pairs, with at least
two distinct points; a closed path is valid. Each consecutive point is one
move without extra interpolation. Use `click` for a tap; do not encode a tap
as a one-point or identical-point path. A series of strokes is a sequential
loop of awaited calls, not `drag_paths` or an array of gestures.

`set_value` requires a real writable UIA Value/RangeValue control.
`perform_secondary_action` supports native patterns such as `invoke`, `toggle`,
`expand`, `collapse`, `select`, `scroll_into_view`; it is not a generic fallback
for clicking a web view. For Chromium combo boxes whose option click has no
effect, focus the control, use arrow/Home/End keys and Enter, then verify.

`toggle` requires TogglePattern and checks its before/after checked state. If
the provider acknowledges the action without a confirmed state change, the call
reports an unconfirmed outcome, not proof that nothing happened. Re-observe
before retrying; if unchanged, click the visible checkbox itself. It does not
silently fall back to Invoke or repeat Toggle. Some Qt tree rows expose a Toggle
pattern that selects a row without changing its check mark.

### Optional input timing

These intervals pace input, not operation deadlines or total-call guarantees.
Use defaults until the particular app/workflow justifies a tested override.

| Field (ms) | Click default | Drag/path default | Accepted range |
| --- | ---: | ---: | --- |
| `move_settle_ms` | 40 | 0 | 0–1000 |
| `press_ms` | 25 | 12 | 0–1000 |
| `step_ms` | not accepted | 8 | 0–100 |
| `after_ms` | 60 | 12 | 0–1000 |

Waits occur after moving to the start, after mouse-down, after each following
move, and after mouse-up respectively. `0` skips that wait; it does not promise
zero latency or prove that the application can process the stream. Drag moves
disable Windows mouse-message coalescing, which does not guarantee that every
application renders every point. Verify actual output when tuning timing.

## Operating rules (both entry points)

1. **UIA first, screenshot second**: default to
   `get_window_state {window, include_text: true, include_screenshot: false}`;
   the `[N]` labels in the tree are `element_index` values and
   `click {window, element_index: N}` needs no screenshot at all. Fall back to
   screenshot pixels only when UIA bounds are missing or untrusted (web pages,
   self-drawn controls).
2. **Do not re-observe mechanically every step**: chain related actions with
   sequential `await`; re-observe only on state changes (dialogs, navigation,
   refresh) or uncertain results.
3. Use `list_windows` results as-is for `window` — HWNDs change; re-list when a
   window is not found.
4. Input success only means **dispatched**; verify important results with UIA
   text (cheaper than screenshots) and take visual proof only when a human
   needs to see it.
