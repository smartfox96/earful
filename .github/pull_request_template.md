## What changed

<!-- What is true after this change that was not before, and why. -->

## How it was checked

<!-- The commands you ran and what they printed. CONTRIBUTING.md, "The checks", lists them. -->

- [ ] `.venvs/lk-1.8.13/bin/python -m pytest -m "not slow"`
- [ ] `scripts/lint.sh`
- [ ] `scripts/check_all.sh`, if `src/` changed
- [ ] A change to behaviour has its deliberate break in `tests/mutants.py`
- [ ] A reference ledger changed only on purpose, and this description says why
- [ ] `CHANGELOG.md` has a line under Unreleased, if a user would notice the change
