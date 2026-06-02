# Review Changes

Review current changes for issues before committing.

## Context

```bash
git diff --stat
```

```bash
git diff
```

## Instructions

Review the diff above for:

1. **Bugs**: Logic errors, off-by-one, null handling, race conditions
2. **Security**: SQL injection, XSS, hardcoded secrets, path traversal
3. **Types**: Missing type hints, incorrect types, Any overuse
4. **Tests**: Are changes tested? Do tests cover edge cases?
5. **Style**: Naming, complexity, code duplication

Provide feedback as:
- 🔴 **Critical**: Must fix before merge
- 🟡 **Warning**: Should fix, potential issues
- 🟢 **Suggestion**: Nice to have improvements

If no issues found, confirm the changes look good.
