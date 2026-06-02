# Handoff

Create a handoff document summarizing current session progress for context continuity.

## Context

```bash
git status --short
```

```bash
git log --oneline -10
```

```bash
git diff --stat HEAD~3..HEAD 2>/dev/null || git diff --stat
```

## Instructions

Create `HANDOFF.md` documenting:

```markdown
# Handoff: [Brief Title]

## Completed
- [What was accomplished this session]

## In Progress
- [Current state of incomplete work]

## Next Steps
- [ ] [Specific actionable tasks to continue]

## Key Files
- `path/to/file.py` - [why it matters]

## Context
[Any important decisions, blockers, or notes for next session]
```

Keep it concise. Focus on what the next session needs to know to continue effectively.
After creating, suggest running `/clear` to start fresh.
