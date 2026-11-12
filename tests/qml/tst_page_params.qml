import QtQuick
import QtTest
import "../../lib/Model.js" as Model

// Model.takePageParams: a nav()'s params reach the page's activated() once, so a page that loads again (a loader
// retry) doesn't repeat its action (e.g. {action: "renew"} from "Re-sign now" starting a second renew build).
// Mirrors AppState.takePageParams; shell.qml's wiring is checked by test_page_params_wiring.py.
// Run by tests/qml/test_qml_core.py.
TestCase {
  name: "PageParams"

  // AppState in miniature: nav() and takePageParams(key) over the same state.
  function makeApp() {
    var app = { page: "home", pageParams: {}, pending: null }
    app.nav = function(key, params) {
      app.pageParams = params || {}
      app.page = key
      app.pending = { page: key, params: app.pageParams }
    }
    app.take = function(key) {
      var r = Model.takePageParams(app.pending, key, key === app.page ? app.pageParams : {})
      app.pending = r.pending
      return r.params
    }
    return app
  }

  function test_actionDeliveredOnce() {
    var app = makeApp()
    app.nav("projects", { action: "renew", path: "/home/me/Projects/CrashLab" })
    compare(app.take("projects"), { action: "renew", path: "/home/me/Projects/CrashLab" })   // shown / loaded
    compare(app.take("projects"), { path: "/home/me/Projects/CrashLab" })                    // loaded again: no action
    compare(app.take("projects"), { path: "/home/me/Projects/CrashLab" })
    compare(app.pageParams.action, "renew")                                                  // pageParams untouched
  }

  function test_deepLinkToAPageNotLoadedYet() {
    var app = makeApp()
    app.nav("device", { tab: "screen", action: "mirror" })
    // the page isn't loaded: shell.qml's onNavigated finds no item and takes nothing; the loader takes it later
    compare(app.take("device"), { tab: "screen", action: "mirror" })
    compare(app.take("device"), { tab: "screen" })
  }

  function test_navigationParamsSurviveAReload() {
    var app = makeApp()
    app.nav("logs", { tab: "crashes" })
    compare(app.take("logs"), { tab: "crashes" })
    compare(app.take("logs"), { tab: "crashes" })
  }

  function test_eachNavDeliversItsOwnAction() {
    var app = makeApp()
    app.nav("projects", { action: "renew" })
    compare(app.take("projects"), { action: "renew" })
    app.nav("projects", { action: "renew" })            // "Re-sign now" pressed again: a new request
    compare(app.take("projects"), { action: "renew" })
    compare(app.take("projects"), {})
  }

  function test_anotherPageNeverGetsThem() {
    var app = makeApp()
    app.nav("projects", { action: "renew" })
    compare(app.take("home"), {})                       // a page that isn't the target or the current one
    compare(app.take("projects"), { action: "renew" })  // still pending for its own page
    app.nav("home")
    compare(app.take("projects"), {})                   // left the page: no stale params for it
  }

  function test_oddInput() {
    compare(Model.takePageParams(null, "home", null), { params: {}, pending: null })
    compare(Model.takePageParams(undefined, "home", undefined), { params: {}, pending: null })
    compare(Model.takePageParams({ page: "home" }, "home", {}), { params: {}, pending: null })
  }
}
