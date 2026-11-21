"""The pytest plugin: how it is loaded, and what its options take."""

import subprocess
import sys

import pytest

from earful.pytest_plugin import _workers


def test_the_workers_option_takes_a_number_of_processes_or_auto():
    assert _workers("auto") is None
    assert (_workers("0"), _workers("3")) == (0, 3)
    for refused in ("-1", "many", ""):
        with pytest.raises(pytest.UsageError, match="a number of processes or auto"):
            _workers(refused)


def test_the_fixture_is_told_what_the_command_line_and_xdist_say(pytester):
    pytester.makeconftest("def pytest_configure(config):\n    config.workerinput = {}\n")
    pytester.makepyfile(
        "def test_it(earful, request):\n"
        "    wanted = request.config.getoption('--earful-workers')\n"
        "    assert earful.spread and earful.workers == wanted\n"
    )
    plain = ("-p", "no:cacheprovider", "-p", "no:asyncio")
    pytester.runpytest_inprocess(*plain).assert_outcomes(passed=1)  # auto: no number
    pytester.runpytest_inprocess(*plain, "--earful-workers", "2").assert_outcomes(passed=1)


def test_loading_the_plugin_does_not_import_either_framework():
    """pytest loads the plugin in every project that has Earful installed."""
    code = (
        "import sys, earful.pytest_plugin\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] in ('livekit', 'pipecat')]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_plugin_is_found_by_its_entry_point(pytester):
    result = pytester.runpytest_subprocess("--help")
    result.stdout.fnmatch_lines(["*--earful-out=DIR*", "*--earful-workers=N*"])
