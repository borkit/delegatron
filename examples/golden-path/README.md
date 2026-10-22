# Golden Path Test

This directory contains a standardized test task for validating the full Delegatron pipeline end-to-end.

## What it tests

The task asks Delegatron to create a simple FastAPI app with:
- A `/health` endpoint
- Full CRUD for `/users` with SQLite
- Pydantic validation
- Complete pytest test suite

## How to run

```bash
# The app is written into the workspace: use an empty directory.
mkdir -p /tmp/golden-path
delegatron run --workspace /tmp/golden-path --max-iterations 5 --max-cost 2.0 \
  "$(cat examples/golden-path/task.json | jq -r .request)"
```

`delegatron run` takes the request text; `--config` is for a `delegatron.toml`,
not this `task.json`. The task's limits are passed as flags above, and its
`expected_files` and `acceptance_criteria` are for checking the result.

## Success criteria

The run passes if:
1. All 8 acceptance criteria are met
2. All generated tests pass in the sandbox
3. The reviewer approves with no blocker issues
4. Completed within 5 iterations and $2.00 budget

## Why this task

- **Small enough** to complete in 1-3 iterations
- **Complex enough** to exercise all three agents (planning, multi-file code gen, review)
- **Deterministically verifiable** via pytest
- **Representative** of real-world usage patterns
