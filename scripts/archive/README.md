# Archived scripts (not production)

Historical Build 002 acceptance/runners live under `build_runners/`.

**Do not execute these for production work.**

Canonical entrypoint:

```text
python -m m3.research.jobs verify-sources
python -m m3.research.jobs acquire
python -m m3.research.jobs recover-unresolved
python -m m3.research.jobs qualify
python -m m3.research.jobs acceptance-002q
```

Acceptance artifacts: `docs/acceptance_archive/`
