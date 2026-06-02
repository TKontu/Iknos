# Lint and Fix

Run ruff to check and fix code issues, then format.

## Instructions

Run the following in sequence:

1. `ruff check . --fix` - Fix auto-fixable lint issues
2. `ruff format .` - Format code
3. `ruff check .` - Show remaining issues that need manual fixes

For any remaining issues:
- Explain what each rule violation means
- Provide the fix
- Apply the fix

After all fixes, run `git diff --stat` to summarize changes made.
