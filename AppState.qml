import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import "services"
import "core"
import "lib/Model.js" as Model

// Root state object. shell.qml creates exactly one (`app`) and hands it to
// every page as `required property var app`. Services live here so their
// state survives page switches.
Item {
  id: root

  // ------------------------------------------------------------ paths
  readonly property string home: Quickshell.env("HOME")
  readonly property string repoPath: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, ""))
  readonly property string helperPath: repoPath + "/bin/iosstudio-helper"
  readonly property string devicedPath: repoPath + "/bin/iosstudio-deviced"
  // Demo mode (`ios-studio --demo`): made-up data from the helper and daemon, and its own
  // settings/state so your real ones are neither shown nor changed.
  readonly property string demo: Quickshell.env("IOSSTUDIO_DEMO") || ""
  readonly property string demoDir: xdgRuntimeDir + "/ios-studio-demo"     // demo_art.demo_dir() in the helper
  // The runtime folder, the helper's rule (helper/iosstudio/device/client.py runtime_base): $IOSSTUDIO_RUNTIME_DIR, else
  // $XDG_RUNTIME_DIR, else /run/user/<uid>. The daemon's own paths (socket folder, log, watch.json) come
  // from its `ready` event: use device.runtimeDir / device.logPath, which fall back to this until then.
  readonly property string xdgRuntimeDir: Quickshell.env("XDG_RUNTIME_DIR") || (_uid !== "" ? "/run/user/" + _uid : "/tmp")
  readonly property string runtimeDir: (Quickshell.env("IOSSTUDIO_RUNTIME_DIR") || xdgRuntimeDir) + (demo !== "" ? "/ios-studio-demo" : "/ios-studio")
  readonly property string _uid: { var m = /^Uid:\s+(\d+)/m.exec(procStatus.text()); return m ? m[1] : "" }
  FileView { id: procStatus; path: "/proc/self/status"; blockLoading: true; printErrors: false }
  // The XDG base folders, the helper's rule (helper/iosstudio/core/appdirs.py): the variable, or the default
  // when it is unset or empty. The helper, daemon and app must agree, e.g. on the daemon's device.json.
  readonly property string xdgConfigHome: Model.xdgHome(Quickshell.env("XDG_CONFIG_HOME"), home, ".config")
  readonly property string xdgStateHome: Model.xdgHome(Quickshell.env("XDG_STATE_HOME"), home, ".local/state")
  readonly property string xdgCacheHome: Model.xdgHome(Quickshell.env("XDG_CACHE_HOME"), home, ".cache")
  readonly property string configDir: demo !== "" ? demoDir + "/config" : xdgConfigHome + "/ios-studio"
  readonly property string stateDir: demo !== "" ? demoDir + "/state" : xdgStateHome + "/ios-studio"
  readonly property string cacheDir: demo !== "" ? demoDir + "/cache" : xdgCacheHome + "/ios-studio"
  readonly property string version: "0.1.0"

  // ------------------------------------------------------------ navigation
  property string page: "home"
  property var pageParams: ({})
  property var visited: ({ home: true })
  property bool userNavigated: false
  property bool shortcutsOpen: false
  property bool windowFocused: false

  // Wired by shell.qml.
  property var toastHost: null
  property var confirmHost: null
  property var pageItems: ({})        // key -> loaded page item

  signal navigated(string page, var params)

  // nav("account", {action: "signin"}) — params reach the page as
  // app.pageParams and through the page's optional activated(params) hook.
  function nav(name, params) {
    var key = Model.resolvePage(name)
    if (key === "") { console.warn("nav: unknown page", name); return false }
    root.pageParams = params || {}
    if (!root.visited[key]) {
      var v = Object.assign({}, root.visited); v[key] = true; root.visited = v
    }
    root.page = key
    root.navigated(key, root.pageParams)
    return true
  }

  function openSetupStep(id) { return nav("setup", { step: id }) }

  // Deep link from the launcher / IPC: "logs", "logs:crashes", "crashes", or (page, tab).
  function navLink(link, tab) {
    var d = Model.parseDeepLink(link)
    if (d.page === "") { console.warn("nav: unknown page", link); return false }
    if (tab) d.params.tab = String(tab).toLowerCase()
    return nav(d.page, d.params)
  }

  function currentPageItem() { return root.pageItems[root.page] || null }

  function cyclePage(delta) {
    var i = Model.pageIndex(root.page)
    var n = Model.PAGES.length
    nav(Model.PAGES[(i + delta + n) % n].key)
  }

  // ------------------------------------------------------------ feedback
  function toast(text, level, opts) {
    if (root.toastHost) root.toastHost.show(text, level || "info", opts)
    else console.log("toast:", level || "info", text)
  }

  // A failed call's error ({code, message, …} or a string) as an error toast, optionally prefixed:
  //   app.cli.call(argv, "", ok, app.toastError)    app.toastError(err, "Couldn't save")
  function toastError(err, prefix) {
    var msg = Model.errorMessage(err)
    toast(prefix ? prefix + ": " + msg : msg, "error")
  }

  // Copy text to the clipboard and say so: copyText(bundleId) → "Copied com.example.app",
  // copyText(path, "the path") → "Copied the path".
  function copyText(text, label) {
    var t = String(text === undefined || text === null ? "" : text)
    Util.execArgv(["wl-copy", "--", t])
    toast("Copied " + (label || (t.length > 60 ? t.substring(0, 57) + "…" : t)))
  }

  // Open a file or folder with its default app (xdg-open). showInFolder(file) opens the folder it's in.
  // For a file:// URL (Image.source) use Util.fileUrl(path) from qs.Commons; is a program on PATH: the
  // CommandCheck singleton (components/CommandCheck.qml).
  function openPath(path) {
    if (!path) return false
    Util.execArgv(["xdg-open", String(path)])
    return true
  }
  function showInFolder(file) {
    var p = String(file || "")
    var i = p.lastIndexOf("/")
    return openPath(i > 0 ? p.substring(0, i) : p)
  }

  // confirm({ title, message, confirmText, cancelText, destructive, glyph, onConfirm, onCancel })
  // Returns a handle for closeConfirm(). A second dialog waits until the first is answered.
  function confirm(opts) {
    if (root.confirmHost) return root.confirmHost.show(opts)
    if (opts && opts.onConfirm) console.warn("confirm host missing; refusing to auto-confirm:", opts.title)
    return null
  }
  // Close a dialog from confirm() unanswered (no callback runs), or drop it from the queue.
  function closeConfirm(handle) { if (root.confirmHost) root.confirmHost.close(handle) }

  // Desktop notification, only when the window isn't focused (and the user
  // hasn't turned them off). glyph: a Model.Glyph value.
  function notify(title, body, glyph) {
    if (root.windowFocused || root.settings.notifications === false) return false
    var argv = ["omarchy-notification-send", "--app-name", "iOS Studio"]
    if (glyph) argv.push("-g", glyph)
    argv.push(String(title))
    if (body) argv.push(String(body))
    Util.execArgv(argv)
    return true
  }

  function refreshAll() {
    var names = ["setup", "toolchain", "account", "device", "projects", "logs"]
    for (var i = 0; i < names.length; i++) {
      var s = root[names[i]]
      if (s && typeof s.refresh === "function") s.refresh()
    }
  }

  // Refresh what the current page shows: the page's own refresh() if it has
  // one, else its service's.
  function refreshCurrent() {
    var item = currentPageItem()
    if (item && typeof item.refresh === "function") { item.refresh(); return }
    var p = Model.pageByKey(root.page)
    if (p && p.service && root[p.service] && typeof root[p.service].refresh === "function") root[p.service].refresh()
    else refreshAll()
  }

  // ------------------------------------------------------------ settings
  readonly property var defaultSettings: ({
    projectRoots: [],             // [] = detected defaults (~/Developer, ~/Projects, ~/Code, … that exist)
    editorCommand: "",            // "" = Omarchy's default editor (omarchy-launch-editor)
    defaultDevice: "",            // UDID, "" = first connected
    notifications: true,
    autoRenew: false,
    autoRenewDays: 2,
    firstRunDone: false
  })
  property var settings: Object.assign({}, defaultSettings)
  property bool settingsLoaded: false

  function setting(key, fallback) {
    var v = root.settings[key]
    return v === undefined ? fallback : v
  }

  function setSetting(key, value) {
    var s = Object.assign({}, root.settings)
    s[key] = value
    root.settings = s
    saveSettings()
  }

  function saveSettings() {
    settingsFile.setText(JSON.stringify(root.settings, null, 2) + "\n")
  }

  FileView {
    id: settingsFile
    path: root.configDir + "/settings.json"
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var parsed = JSON.parse(text() || "{}")
        root.settings = Object.assign({}, root.defaultSettings, parsed)
      } catch (e) {
        console.warn("settings.json is not valid JSON; using defaults")
      }
      root.settingsLoaded = true
    }
    onLoadFailed: root.settingsLoaded = true
  }

  // ------------------------------------------------------------ helpers
  Cli { id: cliObj }
  property alias cli: cliObj

  // ------------------------------------------------------------ services
  property SetupService setup: SetupService { app: root }
  property ToolchainService toolchain: ToolchainService { app: root }
  property AccountService account: AccountService { app: root }
  property DeviceService device: DeviceService { app: root }
  property ProjectService projects: ProjectService { app: root }
  property LogService logs: LogService { app: root }
  property WatchService watch: WatchService { app: root }     // compile on save (one project machine-wide)
  property EditorDebugService editorDebug: EditorDebugService { app: root }   // VS Code / Zed debugger bridges (one per project)
  property HotReloadService hotReload: HotReloadService { app: root }         // hot reload on the iPhone (one session per project)

  // First run / something essential missing → open Setup once, unless the
  // user (or a deep link) already went somewhere.
  Connections {
    target: root.setup
    function onStatusChanged() {
      if (root.setup.status === "needs-setup" && !root.userNavigated && root.page === "home") root.nav("setup")
    }
  }

  function statusJson() {
    var out = { page: root.page, focused: root.windowFocused, version: root.version, services: {} }
    var names = ["setup", "toolchain", "account", "device", "projects", "logs"]
    for (var i = 0; i < names.length; i++) {
      var s = root[names[i]]
      out.services[names[i]] = s ? { status: s.status, summary: s.summary } : null
    }
    return JSON.stringify(out)
  }

  Component.onCompleted: Util.execArgv(["mkdir", "-p", root.configDir, root.stateDir, root.cacheDir])
}
