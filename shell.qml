//@ pragma AppId ios-studio
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import qs.components
import "Model.js" as Model

ShellRoot {
  id: shell

  AppState { id: app }

  ThemeWatcher { id: theme }

  FloatingWindow {
    id: win
    title: "iOS Studio"
    color: Commons.Color.background
    implicitWidth: Style.space(1180)
    implicitHeight: Style.space(780)
    minimumSize: Qt.size(Style.space(600), Style.space(520))
    visible: true

    // Closing the window quits the app (otherwise ShellRoot would keep a
    // windowless process alive and launch-or-focus would find nothing).
    onVisibleChanged: if (!visible) Qt.quit()

    FocusScope {
      id: rootScope
      anchors.fill: parent
      focus: true

      Component.onCompleted: Qt.callLater(function() { rootScope.forceActiveFocus() })

      function pageItem() { return app.currentPageItem() }

      function call(item, fn, arg) {
        if (item && typeof item[fn] === "function") return item[fn](arg)
        return undefined
      }

      // Keys reach here after the focused item (text fields keep their typing).
      Keys.onPressed: function(event) {
        // 1. confirm dialog owns everything while open
        if (confirmDialog.opened) { confirmDialog.handleKey(event); event.accepted = true; return }

        // 2. other overlays (sheets, shortcut sheet): Esc closes the top one
        if (Host.depth > 0) {
          if (event.key === Qt.Key_Escape || (event.text === "?" && app.shortcutsOpen)) { Host.closeTop(); event.accepted = true }
          return
        }

        var mods = event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab)) {
          app.cyclePage((event.modifiers & Qt.ShiftModifier) || event.key === Qt.Key_Backtab ? -1 : 1)
          event.accepted = true; return
        }
        if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_PageDown) { app.cyclePage(1); event.accepted = true; return }
        if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_PageUp) { app.cyclePage(-1); event.accepted = true; return }

        // Esc from inside a text field / focused control: give focus back.
        if (event.key === Qt.Key_Escape && !rootScope.activeFocus) {
          rootScope.forceActiveFocus(); event.accepted = true; return
        }
        if (mods) return

        // 3. the page's own cursor model
        var item = pageItem()
        if (item && typeof item.handleKey === "function" && item.handleKey(event) === true) { event.accepted = true; return }

        // 4. global keys
        if (event.key === Qt.Key_Escape) { call(item, "goBack"); event.accepted = true; return }
        var t = event.text
        if (t >= "1" && t <= "9") {
          var idx = Number(t) - 1
          if (idx < Model.PAGES.length) { app.userNavigated = true; app.nav(Model.PAGES[idx].key) }
          event.accepted = true; return
        }
        if (t === "?") { app.shortcutsOpen = true; event.accepted = true; return }
        if (t === "r") { app.refreshCurrent(); event.accepted = true; return }
        if (t === "/") { call(item, "focusSearch"); event.accepted = true; return }
      }

      RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
          Layout.fillHeight: true
          Layout.preferredWidth: implicitWidth
          app: app
          compact: win.width < Style.space(920)
          current: app.page
          onSelected: function(key) { app.userNavigated = true; app.nav(key); rootScope.forceActiveFocus() }
        }

        Item {
          id: pageArea
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true

          Repeater {
            id: pageLoaders
            model: Model.PAGES
            delegate: Loader {
              id: loader
              required property var modelData
              readonly property bool wanted: !!app.visited[modelData.key]
              readonly property bool current: app.page === modelData.key
              anchors.fill: parent
              visible: current
              opacity: current ? 1 : 0
              Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

              function ensure() {
                if (wanted && !item && status !== Loader.Loading)
                  setSource(Qt.resolvedUrl("pages/" + modelData.file), { app: app })
              }
              // After a failed load: load the file again. The engine caches the broken component
              // by URL, so each retry asks for a new one (?retry=N).
              property int attempts: 0
              function reload() {
                attempts++
                setSource("")
                setSource(Qt.resolvedUrl("pages/" + modelData.file) + "?retry=" + attempts, { app: app })
              }
              onWantedChanged: ensure()
              Component.onCompleted: ensure()
              onLoaded: {
                var m = Object.assign({}, app.pageItems); m[modelData.key] = item; app.pageItems = m
                if (current && typeof item.activated === "function") item.activated(app.pageParams)
              }
              onStatusChanged: if (status === Loader.Error) console.warn("page failed to load:", modelData.file)
            }
          }

          // A page that failed to load (a slice mid-rewrite) shouldn't be a blank void.
          ErrorState {
            anchors.fill: parent
            visible: {
              var it = app.pageItems[app.page]
              return !it && app.visited[app.page] === true && loadCheck.failed
            }
            app: app
            title: "This page couldn't load"
            message: "Part of iOS Studio failed to start. Check `qs log` for details."
            onRetry: {
              for (var i = 0; i < pageLoaders.count; i++) {
                var l = pageLoaders.itemAt(i)
                if (l && l.current) l.reload()
              }
              loadCheck.failed = false
              loadCheck.restart()
            }
            // Runs only while the current page is still loading; 1.5 s without it means it failed.
            Timer {
              id: loadCheck
              property bool failed: false
              interval: 1500
              running: app.visited[app.page] === true && !app.pageItems[app.page]
              onRunningChanged: if (running) failed = false
              onTriggered: failed = !app.pageItems[app.page]
            }
          }
        }
      }

      Connections {
        target: app
        function onNavigated(page, params) {
          var item = app.pageItems[page]
          if (item && typeof item.activated === "function") item.activated(params)
        }
      }

      // ---------------------------------------------------------- overlays
      Item {
        id: overlayLayer
        anchors.fill: parent
        z: 100
        Component.onCompleted: Host.layer = overlayLayer
      }

      ShortcutSheet {
        anchors.fill: parent
        z: 150
        opened: app.shortcutsOpen
        onDismissed: app.shortcutsOpen = false
      }

      ConfirmHost {
        id: confirmDialog
        anchors.fill: parent
        z: 200
        Component.onCompleted: app.confirmHost = confirmDialog
      }

      ToastHost {
        id: toasts
        anchors.fill: parent
        z: 250
        Component.onCompleted: app.toastHost = toasts
      }
    }
  }

  // Window focus → app.windowFocused (gates desktop notifications).
  Binding {
    target: app
    property: "windowFocused"
    value: ToplevelManager.activeToplevel ? ToplevelManager.activeToplevel.appId === "ios-studio" : false
  }

  IpcHandler {
    target: "iosstudio"

    // nav home | nav logs:crashes | nav crashes   (page, optionally ":tab")
    function nav(page: string): string {
      app.userNavigated = true
      return app.navLink(page) ? "ok" : "unknown page: " + page
    }
    // navTab logs crashes
    function navTab(page: string, tab: string): string {
      app.userNavigated = true
      return app.navLink(page, tab) ? "ok" : "unknown page: " + page
    }
    function refresh(): string { app.refreshAll(); return "ok" }
    function status(): string { return app.statusJson() }
    function applyTheme(): string { theme.apply(); return "ok" }
    function toast(text: string, level: string): string { app.toast(text, level || "info"); return "ok" }
  }
}
