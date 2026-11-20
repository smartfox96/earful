"""Shared test setup: ``pytester``, and the reference ledgers of the installed release."""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from earful.scripted.releases import SUPPORT

pytest_plugins = ["pytester"]

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def reference_ledger():
    """Gives a case's committed ledger for the installed livekit-agents, or skips the test."""
    import livekit.agents as lk  # absent where only Pipecat is installed

    def committed(case):
        release = lk.__version__
        # A release the extra admits and docs/compatibility.md does not list has none.
        if not any(GOLDEN.glob(f"lk-{release}.*")):
            pytest.skip(f"livekit-agents {release} has no reference ledgers")
        path = GOLDEN / f"lk-{release}.{case}.ledger.normalized.jsonl"
        assert path.exists(), f"no reference ledger of {case} for livekit-agents {release}"
        return path

    return committed


@dataclass
class _Imported:
    package: str
    version: str

    def _run(self, then, without=None):
        support = SUPPORT[self.package]
        module = {"livekit-agents": "livekit.agents", "pipecat-ai": "pipecat"}[self.package]
        pytest.importorskip(module)
        code = (
            f"import sys, warnings\nimport {module} as framework\n"
            f"framework.__version__ = {self.version!r}\n"
            + (f"sys.modules[{without!r}] = None\n" if without else "")  # its import now raises
            + "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            f"    {then.format(adapter=support.adapter)}\n"
            "for w in caught:\n"
            "    if w.category.__name__ == 'UntestedRelease':\n"
            "        print('warned:', w.message)\n"
            f"print('adapter imported:', '{support.adapter}.harness' in sys.modules)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0 or without, done.stderr
        return support, done

    def warns_once(self):
        support, done = self._run("import {adapter}; import {adapter}.harness")
        tested = ", ".join(support.tested)
        assert done.stdout.splitlines()[-2:] == [
            f"warned: Earful is not tested on {self.package} {self.version}. "
            + f"The tested releases are: {tested}",
            "adapter imported: True",
        ]
        assert done.stdout.count("warned:") == 1

    def is_refused(self):
        support, done = self._run(
            "from earful.cli import main; "
            "print('status:', main(['run', '{adapter}.cases:greeting']))"
        )
        assert done.stdout.splitlines()[-2:] == ["status: 2", "adapter imported: False"]
        (said,) = [line for line in done.stderr.splitlines() if line.startswith("earful: ")]
        assert f"{support.adapter} supports {self.package} from" in said
        assert f"and {self.version} is installed" in said and "\n" not in said

    def lacks(self, module):
        """The last line of the traceback when the framework has no ``module``."""
        _, done = self._run("import {adapter}", without=module)
        assert done.returncode == 1
        return done.stderr.splitlines()[-1]


@pytest.fixture
def imported_on():
    """An adapter imported in a fresh process whose framework reports another release."""
    return _Imported
