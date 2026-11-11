# App icon from any image

An iPhone app's icon is one square PNG, 1024×1024 pixels, without transparency. Getting it wrong doesn't
stop a build: a project without `iconPath` installs with a blank icon, and transparent parts of an icon show
as black on the Home Screen. Set icon… turns any picture into the right file and points `xtool.yml` at it.

## The rules it follows

From Apple's [Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/app-icons)
and xtool's packaging code:

- The icon is a square 1024×1024 PNG. PNG is lossless, so it stays sharp.
- iOS rounds the corners itself. The file is never pre-rounded or masked; a pre-rounded icon gets jagged edges.
- No transparency. iOS fills transparent parts with black, so any alpha is flattened onto a background color.
- Keep the main content centered: the corners are cut off on the iPhone.
- Colors are sRGB. A Display P3 profile in the source is kept; CMYK and other color spaces are converted.
- xtool copies the single file named by `iconPath` (relative to the project folder, and it must end in
  `.png`) into the app and sets `CFBundleIconFile`. There is no asset catalog and no other sizes.

## Fill, Fit and the background

| Fit | What it does | Good for |
|---|---|---|
| Fill | Scales the image to cover the square and crops what sticks out, keeping the middle. | photos |
| Fit | Shows the whole image centered on the background, with a 10% margin on each side. | logos |
| Auto | Fit when the image has transparency, Fill when it doesn't. The default. | |

The background color (white unless you pick another) only shows where the image is transparent and in Fit's
margin. A photo in Fill mode covers it completely.

The answer warns when the source is smaller than 1024×1024 (it is scaled up and may look soft) and when Fill
crops a picture that isn't square.

## Where the file goes

When `iconPath` already names a `.png` inside the project, the new icon replaces that file. Otherwise it is
saved as `Resources/AppIcon.png` (the folder is created) and `iconPath: Resources/AppIcon.png` is added to
`xtool.yml`. Only that one line changes; the rest of `xtool.yml`, comments and order included, stays byte for
byte. A project whose `xtool.yml` is written by its own script (GENERATED near the top) is refused, because
the next run of the script would undo the change: set `iconPath` in that script instead.

The file is written through a temporary file and a rename, so a failed render never leaves a broken icon.
Afterwards the project check runs on it, and its icon findings come back in the answer (none means it passes).
The icon shows on the iPhone after the next Run.

## In the app

On a project card, More (⋮) › Set icon… opens the sheet. A project check warning about the icon has a Set
icon… fix that opens the same sheet (deep link: page `projects`, `{action: "icon", path}`).

- Choose image… opens a file dialog for pictures (PNG, JPEG, WebP, HEIC, GIF, BMP, TIFF, SVG). Until you pick
  one, the preview is an empty rounded tile saying "No image yet".
- The preview is the real 1024×1024 result from a dry run, drawn inside a rounded mask like the Home Screen.
  A caption under it says that iOS rounds the corners itself and the saved file stays square. Beside it are
  the source's size and format, and whether it has transparency.
- Fit (Auto, Fill, Fit) and Background (a few swatches and a hex field; `#rgb` works too) render the preview
  again a moment after the last change. The line under Fit says what Auto picked. The line under
  Background says when the color doesn't show.
- Warnings from the dry run are listed in amber, and a last line says what Set icon will change ("Saves
  Resources/AppIcon.png and adds iconPath to xtool.yml.").
- The current icon is shown small under the preview ("Now"), or "No icon yet".
- Set icon writes the file, shows a toast, closes the sheet and refreshes the card and its settings check.
  It is greyed out until a preview is ready, while it renders, after an error and for a generated
  `xtool.yml`, and its tooltip says why. Errors show the helper's message and its fix button.

Previews and the card's icon are read from disk without the image cache, so a new icon at the same path shows
at once.

## Command line

```bash
iosstudio-helper projects icon --path ~/Developer/WeatherGlance --image ~/Pictures/logo.png        # dry run
iosstudio-helper projects icon --path … --image … --background "#1e90ff" --fit fit --confirm     # write it
iosstudio-helper projects pick-file --kind image --title "Choose an image"                       # {"path": … | null}
```

The answer's `data`: `name`, `path`, `source` (`path`, `width`, `height`, `format`, `hasAlpha`), `fit` (what
Auto resolved to), `background`, `target` (relative), `targetPath`, `replaces`, `yml` (`add`, `change` or
`keep`), `previous` (the old `iconPath` or null), `preview` (a PNG in `~/.cache/ios-studio/icons/`),
`warnings`, `backend` (`pillow` or `imagemagick`), `applied` and `check`. Errors: `usage`, `no-image`,
`bad-image`, `too-large` (over 50 MB or 100 megapixels), `bad-color`, `generated-yml`, `no-image-tool`.

The pixels are done by Pillow when the helper's Python has it, else by the device daemon's Python (which has
Pillow), else by ImageMagick (`magick`, or `convert` from ImageMagick 6), which Omarchy installs. Every step
has a timeout. Previews are pruned on every call: the newest 20 are kept, none older than 7 days.

## AI agents (MCP)

`ios_project_icon(project_path, image_path, background="#ffffff", fit="auto", confirm=false)` is a dry run by
default: it returns `preview_path` and what would change, never image bytes. `confirm=true` writes the icon.
`ios_project_check` points icon findings at it. See [../mcp.md](../mcp.md).

## Demo mode

With `IOSSTUDIO_DEMO` set, the file dialog returns a demo logo and the preview is rendered into the demo
folder. Set icon answers as if it had saved the file (with `demo: true`); nothing in a project changes.

## Code and tests

- Helper: `helper/iosstudio/features/app_icon.py` (`projects icon`, `projects pick-file`); the icon check in
  `helper/iosstudio/features/project_check.py`.
- UI: `components/projects/ProjectIconSheet.qml`, `ProjectService.previewIcon` / `setIcon` / `pickImage`,
  `Model.hexColor` in `lib/Model.js`.
- MCP: `mcp/iosstudio_mcp/tools/app_icon.py`.
- Tests: `tests/helper/test_app_icon.py`, `tests/cli/test_app_icon_cli.py` (fixture images in
  `tests/fixtures/app-icon/`, fake ImageMagick in `tests/support/fake_magick.py`), `tests/qml/tst_model_hex_color.qml`
  and the MCP tool's tests in `tests/mcp/`.
