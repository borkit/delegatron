# CLI Tool Example

A Python CLI tool that organizes files by type. Tests Delegatron's ability to create a complete Click application with argument parsing, file operations, and comprehensive tests.

## What it tests

- Click CLI framework usage
- File system operations (move, mkdir)
- Pattern matching by file extension
- Edge cases (duplicates, dry-run mode)
- Structured pytest test suite

## How to run

```bash
# The tool is written into the workspace: use an empty directory.
mkdir -p /tmp/cli-tool
delegatron run --workspace /tmp/cli-tool "$(cat examples/cli-tool/task.json | jq -r .request)"

# Or with a local preset:
delegatron run --preset local --workspace /tmp/cli-tool "$(cat examples/cli-tool/task.json | jq -r .request)"
```

## Success criteria

The run passes if all 6 acceptance criteria are met and generated tests pass in the sandbox.
