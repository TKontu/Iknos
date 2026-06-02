# Run Tests

Run pytest and analyze results. Argument: optional test path or pattern.

## Context

```bash
git diff --name-only HEAD~1 | grep -E "\.py$" | head -20
```

## Instructions

Run tests based on the argument provided:

- No argument: `pytest -x -v --tb=short`
- File path: `pytest {argument} -v --tb=short`
- Pattern: `pytest -k "{argument}" -v --tb=short`

If tests fail:
1. Show the failure summary
2. Read the failing test file to understand intent
3. Read the source file being tested
4. Explain the root cause
5. Suggest a fix

If all tests pass, report success with coverage summary if available.
