"""pytest plugin: write the failing test node ids (failures, errors, failed subtests, broken files) to $FAILOUT."""

import os

_failed: set[str] = set()
_badfiles: set[str] = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _badfiles.add(report.nodeid)


def pytest_sessionfinish(session):
    out = os.environ.get("FAILOUT")
    if not out:
        return
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"{out}.{wid}", "w") as f:
        for n in sorted(_failed):
            f.write(n + "\n")
        for n in sorted(_badfiles):
            f.write("FILE " + n + "\n")
