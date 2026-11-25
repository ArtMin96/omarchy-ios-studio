"""`test <action>` handlers (stdlib only): flags in, one JSON object or JSON lines out; the work is in project.py,
runner.py and store.py.

  test info --project P                   package, test targets, tested targets, Apple-only imports, last run
  test run --project P [--filter REGEX]   JSON lines: step prepare/build/run, log, progress, diagnostic, test, done
"""
from __future__ import annotations

from iosstudio.core import output

from . import project, runner, store


def cmd_info(flags: dict) -> int:
    root = project.project_root(flags)
    swift = project.find_swift()
    facts = project.package_facts(root, swift)
    return output.ok({
        "project": str(root),
        "package": facts.name,
        "has_tests": bool(facts.test_targets),
        "test_targets": facts.test_targets,
        "tested_targets": facts.tested_targets,
        "host_blockers": project.host_blockers(root, facts) if facts.test_targets else [],
        "last_run": store.last_run(root),
    })


def cmd_run(flags: dict) -> int:
    return runner.run(flags)


GROUPS = {"test": {"info": cmd_info, "run": cmd_run}}
