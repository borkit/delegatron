# Bug Fix Example

Fix a pagination bug in an existing API. Tests Delegatron's ability to understand existing code, locate the bug, apply a targeted fix, and update tests.

## What it tests

- Reading and understanding existing code
- Targeted bug fix (not rewriting everything)
- Correct offset calculation for 1-indexed pagination
- Adding new response fields
- Updating existing test suite

## How to run

```bash
# Create a workspace with the buggy code first, then:
delegatron run --workspace /path/to/buggy-api --max-iterations 3 --max-cost 1.0 \
  "$(cat examples/bug-fix/task.json | jq -r .request)"
```

In a git repository, commit the buggy code first: a run refuses to start with
uncommitted changes (or pass `--allow-dirty`), and works on its own
`delegatron/run-<run-id>` branch.

## Success criteria

The run passes if the offset calculation is fixed, total_pages is added, and all tests pass.
