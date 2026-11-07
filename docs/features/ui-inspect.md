# Read the screen, press elements, run an accessibility audit

An agent that changed your app's code wants to know if the change works, and a screenshot only shows
pixels. iOS Studio reads the app on the iPhone's screen as data instead: every element VoiceOver can
reach, with its label, value and role. It can press one of them and say what changed, and it can run
Apple's accessibility audit on the screen. You get the same in the window under iPhone › Screen ›
Elements and Audit.

It works on the real iPhone. Nothing is installed on the phone, and it needs neither WebDriverAgent
nor Xcode.

## How it works

The iPhone has an accessibility inspector service for Apple's Accessibility Inspector
(`com.apple.accessibility.axAuditDaemon.remoteserver.shim.remote`). iOS Studio reaches it over the
same RSD tunnel as the other iPhone tools, through pymobiledevice3's `AccessibilityAudit`. It needs
Developer Mode and an unlocked phone.

- **It has to be pointed at the app.** The inspector only answers after `deviceSetAuditTargetPid:`
  points it at the app's process, and only while that app is in front. Pointed at an app in the
  background, the focus never moves and the audit finds nothing. So every command first finds the
  app: the `bundleId` you pass, or the newest running apps in turn (up to 5) when you don't pass one.
- **The list is a focus walk.** iOS Studio moves the VoiceOver-style focus to the first element, then
  to the next one until it comes back to an element it has seen. The result is a flat list in reading
  order, not a nested tree. Elements of other processes that the focus passes (the keyboard, an
  alert) are left out and counted.
- **An element knows its process.** The first 4 bytes of an element's reference (little-endian) are
  the pid of the app it belongs to. iOS Studio uses that to check that the focus is in the right app
  (the wrong-app guard below) and to match audit issues to elements of the last walk.
- **Pressing.** A press is the accessibility "Activate" action (`AXAction-2010`), the same as a
  VoiceOver double tap. pymobiledevice3 11.49's `perform_press` is silently ignored on iOS 27.3, so iOS
  Studio sends the element in the format it uses to read attributes, which the phone accepts.

Element ids (`e1`, `e2`, …) number the elements of the last walk on that iPhone. They go stale when
the screen changes or the connection drops.

## What you get

An element:

```json
{"id": "e4", "role": "button", "label": "Increment", "value": null, "hint": "Adds one to the count",
 "identifier": "incrementButton", "traits": ["button"], "frame": null, "enabled": true}
```

`role` is the first of searchField, toggle, button, link, adjustable, tabBar, keyboardKey, header,
image, staticText or textField found in the traits. `identifier` is the `accessibilityIdentifier`
from the app's code. `frame` is `{x, y, w, h}` in screen points when the phone reports one.

A press reports what changed between the walk before and the walk after it:

```json
{"added": [], "removed": [], "changedValues": [{"label": "countLabel", "from": "Count: 1", "to": "Count: 0"}]}
```

When an element's label itself changed, as with a `Text("Count: \(count)")`, it is named by its
identifier.

An audit issue has a `type`, a plain message, the element's label and frame, and details such as the
colours and font size of a contrast problem. These types run on iOS 27.3:

| Type | Apple's name | What it finds |
|---|---|---|
| `missing-label` | element description | an element without a useful label |
| `contrast` | contrast | text or an icon too faint against its background |
| `hit-region` | hit region | a touch target smaller than 44×44 points |
| `element-detection` | element detection | something on screen VoiceOver can't reach |
| `clipped-text` | clipped text | text cut off at larger text sizes |
| `traits` | traits | traits that look wrong |
| `dynamic-type` | dynamic type | text that doesn't grow with the text size setting |

A type the phone doesn't offer is reported as `unsupported`, not as an error.

## Timings on a real iPhone

Measured on an iPhone 17 Pro with iOS 27.3, on a test screen of 10 elements:

| | About |
|---|---|
| read the screen | 5 s (about 0.36 s per element) |
| press by id | 8 s (press, then read the screen again) |
| press by label | 12 s (it reads the screen first, to find the label) |
| audit | 5 s |
| refusal because another app is in front | 3.3 s |

## Limits

| | |
|---|---|
| elements per walk | 150 by default, 400 at most |
| time for one walk | 12 s by default, between 2 and 20 s |
| a whole press | about 20 s (the label walk before it takes at most 8 s) |
| an audit | 18 s, then `timedOut` with partial results |
| apps tried as "the app in front" without a bundle id | the 5 newest running apps |

A walk stops early when it reaches the element limit (`max`), runs out of time (`timeout`), when the
focus doesn't move (`no-focus`, often an alert or the keyboard) or when it passes more than 20
elements of other processes (`foreign`). The result then says `complete: false`.

- **No outlines on iOS 27.3.** The phone reports no frame for an element (none of the frame
  attributes answer), so element rows aren't outlined on the phone picture. Audit issues have frames
  and are outlined.
- **Only what VoiceOver reaches.** Elements hidden from accessibility, and views without an element,
  aren't in the list. That's also what VoiceOver users get.
- **Only the screen showing now.** Content further down a scroll view is in the list only if
  VoiceOver can reach it. The audit checks the current screen.
- **Typing text and swipes aren't supported.** A press is the only action. Typing and gestures
  belong to the WebDriverAgent work in #17.

## The wrong-app guard

A press taps the real phone, in whatever app is in front. So:

- Pass the bundle id (`bundle_id` in MCP, `bundleId` in the daemon). The command then fails with
  `wrong-app` when that app isn't installed, isn't running or isn't the app in front, instead of
  reading or pressing another app. The plain id and the `XTL-<TEAM>.` form of a free account both work.
- A press by id targets the app of the last walk. If another app came to the front since, it fails
  (`wrong-app`, or `stale-element` when the same bundle runs as a new process).
- Every command checks that the first focused element belongs to the app's pid before it reads or
  presses anything.

## Errors

| Code | When |
|---|---|
| `no-element` | No element has that label. The error lists close labels. |
| `ambiguous-label` | Several elements have that label. The error lists their ids; press one by id. |
| `stale-element` | The id isn't in the last walk, there was no walk yet, or the app in front changed. Read the screen again. |
| `press-failed` | The phone didn't accept the press. |
| `wrong-app` | The app named by the bundle id isn't installed, isn't running or isn't in front. |
| `no-app-in-front` | Without a bundle id, none of the newest running apps answered. The phone may be locked or on the home screen. |
| `ax-unavailable` | The accessibility service couldn't be reached. Unlock the phone and check Developer Mode. |
| `bad-args` | No id or label for a press, a bad `max`, `timeout` or `types`. |

## In the app

iPhone › Screen has three tabs beside the phone: Live view, Elements and Audit.

- **Elements**: Read the screen lists the elements of the app in front, in reading order, each with
  a role glyph, its label (or "No label"), its value, role and identifier. The filter (`/`) narrows by
  label, value, role or identifier. The cursor row has a Press button; Enter presses too. Press taps
  the element on the real phone, then says what changed ("Changed: countLabel Count: 1 → Count: 0")
  and reads the screen again. The footer names the app, the count and how long the read took, and a
  partial read says why it stopped.
- **Audit**: Run audit lists the issues grouped by type, most frequent first. Clicking an issue (or
  Enter) shows its element in Elements when the last read has it. Types the phone can't check are
  listed under the issues.
- **The outline**: the cursor row's frame is outlined on the phone picture. Each read also takes a
  screenshot, so the outline matches what is shown; its size gives the screen size in points when the
  phone doesn't report one. On iOS 27.3 only audit issues are outlined, and the Elements footer says so.
- `j`/`k` or the arrow keys move the cursor, `g`/`G` jump to the first and last row.

See [Read the screen and check accessibility](../guide.md#read-the-screen-and-check-accessibility) in
the guide.

## MCP

The tools an agent uses to check its own change, without a human:

```
ios_run(project_path)                     build, install and launch
ios_ui_tree(bundle_id=…)                  what's on screen: ids, roles, labels, values
ios_ui_press(element_id='e4', bundle_id=…)   press it, see what changed
ios_ui_tree(…) or ios_screenshot()        check the new screen
ios_a11y_audit(bundle_id=…)               accessibility issues on this screen
```

- `ios_ui_tree(bundle_id=, filter=, roles=, interactive_only=, cursor=, max_bytes=, max_elements=150)`:
  the elements in reading order. `filter` matches label, value, identifier or role; `roles` keeps
  roles or traits such as `['button']`; `interactive_only` keeps buttons, links, fields, switches,
  sliders, tabs and cells. Results are paged at about 12 KB: `next_cursor` pages through the same
  walk (kept 120 s) without walking again, and a call without `cursor` reads the screen again. Labels,
  values and hints are cut at 120 characters. `complete=false` with `stopped_by` and a `note` says why
  a walk is partial. `frame` is `[x, y, w, h]` when the phone reports one.
- `ios_ui_press(element_id= or label=, match='exact', bundle_id=)`: exactly one of `element_id` and
  `label`. `match='exact'` also matches the `accessibilityIdentifier`; `contains` matches part of the
  label. The result has `pressed`, `added`, `removed`, `changed_values` (25 of each at most),
  `elements_after`, `after_complete` and `screen_changed`. A press always forgets the cached walk, so
  the next `ios_ui_tree` reads the screen again.
- `ios_a11y_audit(bundle_id=, types=, max_issues=50)`: `counts` per type (most frequent first),
  `total`, and up to `max_issues` (200 at most) issues with label, frame and details, grouped by type.
  `types` takes the slugs above. `supported` and `unsupported` say what the phone checked, and
  `timed_out=true` marks partial results. The audit runs as a job: a call that runs out of time returns
  `state: "running"` and a `job_id`; call `ios_a11y_audit(job_id=…)` again for the result.

Pass `bundle_id` to all three, so a call fails with `wrong-app` instead of reading or pressing another
app. Errors carry the code and a next step, such as "Call ios_ui_tree again and use a fresh id" for
`stale-element`.

## Demo mode

With `IOSSTUDIO_DEMO=1` (or `ios-studio --demo`), the app in front is HabitTracker once it has been
launched, else WeatherGlance (what the demo screenshot shows). Both screens have frames and a 390×844
screen, so element rows are outlined in demo mode.

- WeatherGlance: pressing Units switches between °C and °F. Its audit finds low contrast on the hourly
  forecast, a chart without a label and text that doesn't grow with Dynamic Type.
- HabitTracker: pressing a habit marks it done (its value changes), Add habit adds a row. Its audit
  finds a small Settings button and clipped text.

`wrong-app`, `stale-element`, `no-element` and `ambiguous-label` fail as they would on the phone. The
demo phone offers six of the types above; asking it for `traits` reports that type as unsupported.

## Checked on a real iPhone

On an iPhone 17 Pro with iOS 27.3: reading a 10-element screen, pressing by id and by label (the
button fires and the change is reported), the audit with all seven types, issues matched to elements,
the wrong-app refusal and missing frames. The captures are in `tests/fixtures/ui-inspect/real-*.json`.

Not checked yet:

- iOS 17 to 26.
- A screen with more than 150 elements on a real phone.
- The phone locking while a walk or press runs.

## Code

`helper/iosstudio/features/ui_inspect.py` (no device: plain JSON from the phone's objects, the walk, elements, label
matching, the change summary, audit types and issues), `helper/iosstudio/device/features/ui/real.py` (`ui.tree`,
`ui.press`, `ui.audit`: the session, targeting the app, the walk, the press),
`helper/iosstudio/device/features/ui/demo.py` (the demo screens), `mcp/iosstudio_mcp/tools/ui_inspect.py`
(`ios_ui_tree`, `ios_ui_press`, `ios_a11y_audit`), `mcp/iosstudio_mcp/core/errors.py` (next steps for
the error codes), `components/device/DeviceInspectView.qml`, `components/device/DeviceInspectRow.qml`,
`components/device/DeviceScreenView.qml` (the tabs), `components/device/DevicePhoneFrame.qml` (`highlight`),
`pages/DevicePage.qml` (keys), `services/DeviceService.qml` (60 s timeouts for `ui.*`), `lib/Model.js`
(glyphs). Tests: `tests/helper/test_ui_inspect.py` and `tests/mcp/test_mcp_ui_inspect.py`, with fixtures in
`tests/fixtures/ui-inspect/`.
