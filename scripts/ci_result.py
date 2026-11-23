"""Did every job of checks.yml do what the scope asked of it? Exits 1 if not.

  DUE='["lint"]' NEEDS='<toJSON(needs)>' EXPLAINED='"<text>"' python3 scripts/ci_result.py

DUE is the list scripts/ci_scope.py printed, NEEDS each job's result as GitHub gives it,
EXPLAINED what the scope said about the change. A job the scope asks for has to have
passed, and every other job has to have been skipped.
"""

from __future__ import annotations

import json
import os
import sys

JOBS = (
    "scope",
    "prose",
    "lint",
    "wheel",
    "quickstart",
    "report",
    "livekit",
    "pipecat",
    "bare",
    "control",
    "breaks",
)


def problems(due_text: str, needs_text: str) -> tuple[list[str], list[str]]:
    """What is wrong, and a line for each job that says what it did."""
    wrong: list[str] = []
    try:
        needs = json.loads(needs_text)
    except json.JSONDecodeError:
        return [f"the results of the jobs are not JSON: {needs_text!r}"], []
    results = {job: (needs.get(job) or {}).get("result", "") for job in needs}
    wrong += [
        f"no rule here says when the job {job!r} has to run" for job in results if job not in JOBS
    ]
    wrong += [f"the result of the job {job!r} is missing" for job in JOBS if job not in results]
    if results.get("scope") != "success":
        return [*wrong, f"the scope job's result is {results.get('scope')!r}"], []
    try:
        due = json.loads(due_text)
    except json.JSONDecodeError:
        return [*wrong, f"the scope gave no list of jobs: {due_text!r}"], []
    if not isinstance(due, list) or any(job not in JOBS[1:] for job in due):
        return [*wrong, f"the scope named jobs it cannot: {due!r}"], []
    said = []
    for job in JOBS[1:]:
        got = results.get(job, "")
        if job in due:
            said.append(f"{job}: {got}")
            if got != "success":
                wrong.append(f"{job} had to run and its result is {got!r}")
        elif got != "skipped":
            wrong.append(f"{job} was not asked for and its result is {got!r}")
    return wrong, said


def main() -> int:
    wrong, said = problems(os.environ.get("DUE", ""), os.environ.get("NEEDS", ""))
    try:
        explained = json.loads(os.environ.get("EXPLAINED", '""'))
    except json.JSONDecodeError:
        explained = ""
    report = explained + ("Jobs:\n" + "".join(f"  {line}\n" for line in said) if said else "")
    print(report, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write(f"```\n{report}```\n")
    for line in wrong:
        print(line, file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
