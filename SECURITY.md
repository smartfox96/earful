# Security

## Reporting a vulnerability

Report a vulnerability privately:
[open a new advisory](https://github.com/smartfox96/earful/security/advisories/new), or
choose "Report a vulnerability" on the repository's Security tab. Only you and the
maintainer can read the report. Do not open a public issue for it.

Say which version of Earful and of the framework you ran, the command, and what
happened. Attach a scenario, a ledger or a bundle that shows the problem if you can.

Expect a first answer within a week. A fix ships as a new release, and the advisory is
published with it. Fixes go into the latest release. Earlier releases are not patched.

## What is in scope

Earful is a test tool. It runs on a developer's machine or in CI, with the permissions of
whoever starts it.

**A scenario is code, and so is a bundle.** `earful run`, `earful search`,
`earful crosscheck`, `earful init` and the pytest fixture import and run the Python
module they are given as `module:name`. A kept bundle names its scenario the same way,
and `earful replay` imports that module from the directory the command runs in. Treat
a bundle someone sends you as you would a test file they send you: read it before you
run it.

In scope:

- A ledger, a case file or a bundle manifest that makes `earful check` or
  `earful report` run code, or read or write a file outside the paths it was given.
  Neither command imports a scenario.
- A report that runs script taken from the ledger it draws, or that loads anything from
  outside itself. A report is one HTML page that opens offline.
- The build and release of the package: the workflows under `.github/workflows/` and
  what they publish to PyPI.

Out of scope:

- What a scenario module, an agent or a tool does when Earful runs it.
- Defects in livekit-agents or pipecat-ai. Report those to their projects.
  `CONTRIBUTING.md`, "Reporting what Earful finds", says how to tell which side a failure
  is on.
