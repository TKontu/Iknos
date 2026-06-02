# Commit, Push, and Create PR

Commit staged changes, push to remote, and create a pull request.

## Context

```bash
git status --short
```

```bash
git diff --cached --stat
```

```bash
git log --oneline -5
```

```bash
git branch --show-current
```

## Instructions

1. **Secrets check** - Scan staged files for exposed secrets:
   - API keys, tokens, passwords, private keys, database URLs
   - If found: STOP and warn user, do not commit
2. Review the staged changes above
3. Write a concise commit message following conventional commits (feat/fix/refactor/docs/test/chore)
4. Commit the changes
5. Push to the current branch
6. Create a PR with:
   - Clear title summarizing the change
   - Brief description of what and why
   - Link any related issues if mentioned in commits

If no changes are staged, inform the user and suggest `git add` commands.
