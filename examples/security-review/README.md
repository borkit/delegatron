# Security Review Example

A security-focused code review of a Flask application. Tests the read-only review pipeline (`delegatron review`).

## What it tests

- Read-only code review mode (no code generation)
- Security-focused analysis
- Structured issue reporting with severities
- File and line references in findings
- Actionable fix recommendations

## How to run

```bash
# Review your own Flask app
delegatron review --workspace /path/to/flask-app --focus security

# Review with improvement suggestions from the architect
delegatron review --workspace /path/to/flask-app --focus security --with-suggestions

# Review a remote repo
delegatron review --workspace https://github.com/org/flask-app.git --focus security
```

## What to expect

The review produces a structured `ReviewReport` with issues categorized by severity (blocker, major, minor, nit). No code changes are made — this is purely diagnostic.
