#!/usr/bin/env python3
"""A stand-in for `swift package …` used by tests/test_packages.py and tests/test_mcp_packages.py (no Swift needed).

It understands the small manifests the tests write: `.package(url: "…", from: "…")` / `.package(path: "…")`
entries, `.target(name: "…")` / `.testTarget(…)`, `.library(name: "…", targets: [...])` and
`.product(name: "…", package: "…")`. The behaviour comes from FAKE_SWIFT_PACKAGES, a JSON object
{identity: {"products": [...], "version": "1.2.3", "update": "1.3.0", "fail": "clone|resolve"}}, and
FAKE_SWIFT_SLEEP (seconds `resolve`/`update` hang after writing a partial Package.resolved, for cancel tests;
the process writes its pid to FAKE_SWIFT_PIDFILE first). A manifest containing SYNTAX ERROR fails to dump.

    package [--package-path P] [--scratch-path S] [--disable-automatic-resolution] <command> …
"""
import json
import os
import re
import sys
import time

ROOT_FLAGS = {"--package-path", "--scratch-path"}


def ident(loc: str) -> str:
    s = loc.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return re.split(r"[/:]", s)[-1].lower()


def config() -> dict:
    return json.loads(os.environ.get("FAKE_SWIFT_PACKAGES") or "{}")


def deps(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r'\.package\((url|path): "([^"]+)"(?:, (from|exact|branch|revision): "([^"]+)")?\)', text):
        kind, loc, rule, ver = m.groups()
        out.append({"kind": kind, "loc": loc, "rule": rule, "version": ver, "identity": ident(loc)})
    return out


def dump(pkg_dir: str) -> int:
    text = open(os.path.join(pkg_dir, "Package.swift")).read()
    if "SYNTAX ERROR" in text:
        line = text[:text.index("SYNTAX ERROR")].count("\n") + 1
        sys.stderr.write(f"{pkg_dir}/Package.swift:{line}:5: error: cannot find 'SYNTAX' in scope\n")
        sys.stderr.write(f"error: invalid manifests at [{pkg_dir}]\n")
        return 1
    name = re.search(r'Package\(\s*name: "([^"]+)"', text)
    dependencies = []
    for d in deps(text):
        if d["kind"] == "path":
            dependencies.append({"fileSystem": [{"identity": d["identity"], "path": os.path.normpath(
                os.path.join(pkg_dir, d["loc"]))}]})
        else:
            req = {"from": {"range": [{"lowerBound": d["version"], "upperBound": f"{int(d['version'].split('.')[0]) + 1}.0.0"}]},
                   "exact": {"exact": [d["version"]]}, "branch": {"branch": [d["version"]]},
                   "revision": {"revision": [d["version"]]}}[d["rule"]]
            dependencies.append({"sourceControl": [{"identity": d["identity"], "location": {"remote": [
                {"urlString": d["loc"]}]}, "requirement": req}]})
    targets = []
    starts = list(re.finditer(r'\.(target|testTarget|executableTarget)\(\s*name: "([^"]+)"', text))
    for k, m in enumerate(starts):
        kind, tname = m.groups()
        rest = text[m.end():starts[k + 1].start() if k + 1 < len(starts) else len(text)]
        tdeps = [{"product": [p, pk, None, None]} for p, pk in
                 re.findall(r'\.product\(name: "([^"]+)", package: "([^"]+)"\)', rest)]
        targets.append({"name": tname, "type": {"target": "regular", "testTarget": "test",
                                                "executableTarget": "executable"}[kind], "dependencies": tdeps})
    products = [{"name": n, "type": {"library": ["automatic"]}, "targets": re.findall(r'"([^"]+)"', t)}
                for n, t in re.findall(r'\.library\(\s*name: "([^"]+)",\s*targets: \[([^\]]*)\]', text)]
    print(json.dumps({"name": name.group(1) if name else "Pkg", "dependencies": dependencies, "targets": targets,
                      "products": products}))
    return 0


def write_resolved(root: str, pins: dict) -> None:
    data = {"originHash": "0" * 64, "pins": [
        {"identity": i, "kind": "remoteSourceControl", "location": p["url"],
         "state": {"revision": (i * 40)[:40].replace("-", "a"), "version": p["version"]}}
        for i, p in sorted(pins.items())], "version": 3}
    with open(os.path.join(root, "Package.resolved"), "w") as fh:
        json.dump(data, fh, indent=2)


def read_pins(root: str) -> dict:
    try:
        data = json.load(open(os.path.join(root, "Package.resolved")))
    except OSError:
        return {}
    return {p["identity"]: {"url": p["location"], "version": p["state"].get("version")} for p in data["pins"]}


def checkout(root: str, identity: str, url: str) -> None:
    d = os.path.join(root, ".build", "checkouts", identity)
    os.makedirs(d, exist_ok=True)
    prods = config().get(identity, {}).get("products", [identity.title().replace("-", "")])
    libs = "".join(f'        .library(name: "{p}", targets: ["{p}"]),\n' for p in prods)
    with open(os.path.join(d, "Package.swift"), "w") as fh:
        fh.write(f'// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n'
                 f'    name: "{identity}",\n    products: [\n{libs}    ]\n)\n')
    state_p = os.path.join(root, ".build", "workspace-state.json")
    try:
        state = json.load(open(state_p))
    except OSError:
        state = {"object": {"dependencies": []}, "version": 7}
    state["object"]["dependencies"] = [x for x in state["object"]["dependencies"]
                                       if x["packageRef"]["identity"] != identity]
    state["object"]["dependencies"].append({"packageRef": {"identity": identity, "kind": "remoteSourceControl",
                                                           "location": url, "name": identity},
                                            "state": {"name": "sourceControlCheckout"}, "subpath": identity})
    json.dump(state, open(state_p, "w"))


def hang() -> None:
    pidfile = os.environ.get("FAKE_SWIFT_PIDFILE")
    if pidfile:
        open(pidfile, "w").write(str(os.getpid()))
    secs = float(os.environ.get("FAKE_SWIFT_SLEEP") or 0)
    if secs:
        print("Fetching https://example.com/slow", flush=True)
        time.sleep(secs)


def resolve(root: str, update: bool = False, only: str | None = None) -> int:
    text = open(os.path.join(root, "Package.swift")).read()
    cfg = config()
    pins = read_pins(root)
    new = dict(pins)
    for d in deps(text):
        if d["kind"] != "url":
            continue
        i, c = d["identity"], cfg.get(d["identity"], {})
        print(("Updating " if update else "Fetching ") + d["loc"], flush=True)
        if c.get("fail") == "clone":
            print(f"error: Failed to clone repository {d['loc']}:\n    remote: Repository not found.\n"
                  f"    fatal: repository '{d['loc']}/' not found", flush=True)
            return 1
        if c.get("fail") == "resolve":
            print(f"error: Dependencies could not be resolved because no versions of '{i}' match the requirement "
                  f"{d['version']}..<99.0.0 and root depends on '{i}' {d['version']}..<99.0.0.", flush=True)
            return 1
        version = c.get("version", "1.2.3")
        if update and (only in (None, i)) and c.get("update"):
            version = c["update"]
        elif i in pins and not update:
            version = pins[i]["version"]
        new[i] = {"url": d["loc"], "version": version}
        print(("Updated " if update else "Fetched ") + d["loc"] + " (0.10s)", flush=True)
        print(f"Computed {d['loc']} at {version} (0.10s)", flush=True)
        checkout(root, i, d["loc"])
    for i in list(new):
        if i not in {d["identity"] for d in deps(text)}:
            new.pop(i)
    if os.environ.get("FAKE_SWIFT_SLEEP"):
        write_resolved(root, {**new, "partial-pin": {"url": "https://example.com/partial", "version": "0.0.1"}})
        hang()
    write_resolved(root, new)
    if update and new == pins:
        print("Everything is already up-to-date")
    return 0


def add_dependency(root: str, args: list[str]) -> int:
    p = os.path.join(root, "Package.swift")
    text = open(p).read()
    loc = args[0]
    if any(d["identity"] == ident(loc) for d in deps(text)):
        print(f"error: unable to add dependency '{loc}' because it already exists in the list of dependencies")
        return 1
    if "--type" in args:
        entry = f'.package(path: "{loc}")'
    else:
        flag = args[1]
        rule = {"--from": "from", "--exact": "exact", "--branch": "branch", "--revision": "revision",
                "--up-to-next-minor-from": "from"}[flag]
        entry = f'.package(url: "{loc}", {rule}: "{args[2]}")'
    if "    dependencies: [\n" in text:
        text = text.replace("    dependencies: [\n", f"    dependencies: [\n        {entry},\n", 1)
    else:
        text = text.replace("    targets: [", f"    dependencies: [\n        {entry},\n    ],\n    targets: [", 1)
    open(p, "w").write(text)
    print("Updating package manifest at Package.swift... done.")
    return 0


def add_target_dependency(root: str, args: list[str]) -> int:
    prod, target, pkg = args[0], args[1], args[args.index("--package") + 1]
    p = os.path.join(root, "Package.swift")
    text = open(p).read()
    m = re.search(r'\.target\(\s*name: "' + re.escape(target) + r'"', text)
    if not m:
        print(f"error: unable to find target named '{target}' in package")
        return 1
    line = f'.product(name: "{prod}", package: "{pkg}")'
    after = text[m.end():]
    if after.lstrip().startswith(",") and "dependencies: [" in after.split(")")[0]:
        text = text[:m.end()] + after.replace("dependencies: [", f"dependencies: [\n                {line},", 1)
    else:
        text = text[:m.end()] + f",\n            dependencies: [\n                {line},\n            ]" + after
    open(p, "w").write(text)
    print("Updating package manifest at Package.swift... done.")
    return 0


def show_dependencies(root: str) -> int:
    pins = read_pins(root)
    print(json.dumps({"identity": "root", "dependencies": [{"identity": i, "dependencies": []} for i in pins]}))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "package":
        print("fake swift: only `swift package …`", file=sys.stderr)
        return 2
    args = argv[1:]
    pkg_dir = os.getcwd()
    rest = []
    i = 0
    while i < len(args):
        if args[i] in ROOT_FLAGS:
            if args[i] == "--package-path":
                pkg_dir = args[i + 1]
            i += 2
            continue
        if args[i] == "--disable-automatic-resolution":
            i += 1
            continue
        rest.append(args[i])
        i += 1
    cmd, cargs = rest[0], rest[1:]
    if cmd == "dump-package":
        return dump(pkg_dir)
    if cmd == "add-dependency":
        return add_dependency(pkg_dir, cargs)
    if cmd == "add-target-dependency":
        return add_target_dependency(pkg_dir, cargs)
    if cmd == "resolve":
        return resolve(pkg_dir)
    if cmd == "update":
        return resolve(pkg_dir, update=True, only=cargs[0] if cargs else None)
    if cmd == "show-dependencies":
        return show_dependencies(pkg_dir)
    print(f"fake swift: unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
