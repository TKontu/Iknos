# Takeoff
Review the project state and handoff document to plan how to continue work effectively.

## Context
```bash
cat HANDOFF.md 2>/dev/null || echo "No HANDOFF.md found"
```
```bash
git status --short
```
```bash
git log --oneline -5
```
```bash
ls -la
```

## Instructions
Review the project state and create a continuation plan:

1. **Read the Handoff** - If `HANDOFF.md` exists, use it as the primary context source
2. **Assess Current State** - Check git status, recent commits, and project structure
3. **Identify Priorities** - Determine what to work on first based on:
   - In-progress items from handoff
   - Next steps listed in handoff
   - Any failing tests or obvious issues

Then provide a response in this format:

```markdown
# Takeoff Plan

## Quick Summary
[1-2 sentence overview of where we left off]

## Priority Tasks
1. [ ] [Most important task to tackle first]
2. [ ] [Second priority]
3. [ ] [Third priority]

## Ready to Start
[Specific first action to take - be concrete, e.g., "Open `src/auth.py` and fix the TODO on line 42"]
```

After presenting the plan, ask: **"Ready to begin? Which task should we start with?"**

If no `HANDOFF.md` exists, do a quick project reconnaissance:
- Check README.md for project context
- Look at recent git history
- Identify the main entry points
- Suggest creating a handoff document for future sessions
