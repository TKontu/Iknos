# Orchestrate - Multi-Agent Workflow

You are the **ORCHESTRATOR (Opus)**. You plan. Sonnet agents execute.

## Context
```bash
ls -la docs/TODO-*.md 2>/dev/null
```
```bash
git branch -a | head -20
```
```bash
gh pr list --state open 2>/dev/null | head -10
```

## Your Role

1. **Plan** - Break work into independent, well-specified tasks
2. **Assign** - Create TODO files for each agent with complete specs
3. **Coordinate** - Track agent progress, merge PRs, handle conflicts
4. **Review** - Verify completed work meets requirements

## Architecture

```
[Opus - This Session]
    |
    ├── Plans work
    ├── Creates docs/TODO-agent-*.md
    ├── Pushes to main
    └── Reviews PRs

[Agent 1 - Separate Session]     [Agent 2 - Separate Session]
    |                                |
    ├── Pulls main                   ├── Pulls main
    ├── Reads TODO-agent-1.md        ├── Reads TODO-agent-2.md
    ├── Creates feat/ branch         ├── Creates feat/ branch
    ├── Executes (TDD)               ├── Executes (TDD)
    └── Creates PR                   └── Creates PR
```

## Agent Setup (One-Time per Agent Folder)

Each agent needs a separate folder with executor CLAUDE.md:

```bash
# Clone/copy the repo to agent folder
cp -r /path/to/knowledge_extraction /path/to/knowledge_extraction-agent-1

# Replace CLAUDE.md with executor version
cp .claude/templates/CLAUDE-executor.md /path/to/knowledge_extraction-agent-1/CLAUDE.md
```

## Workflow Commands

| Command | Purpose |
|---------|---------|
| `/orchestrate` | This - remind yourself of the workflow |
| `/assign-agent` | Create TODO file and agent instructions |
| `/update-todos` | Update master TODO with completed work |

## Task Assignment Checklist

When creating a TODO file for an agent:

- [ ] **Independent** - Agent can complete without blocking on other agents
- [ ] **Complete spec** - All requirements stated, no implicit knowledge needed
- [ ] **File paths** - Exact files to create/modify
- [ ] **Test cases** - What tests to write
- [ ] **Constraints** - What NOT to do, dependencies
- [ ] **Acceptance criteria** - How to verify it's done

## Agent Startup Prompt

When starting an agent session, paste:

```
I am an executor agent. My ID is {agent-id}.
Pull main and read my TODO file at docs/TODO-{agent-id}.md.
Execute the tasks using TDD. Create PR when done.
```

## Coordination

**Handling Conflicts:**
- Agents should work on independent files
- If conflict likely, assign sequentially (agent-1 finishes, then agent-2)
- Review PRs promptly to minimize divergence

**Progress Tracking:**
- Check open PRs: `gh pr list --state open`
- Each completed PR = one TODO file done
- Update master TODO after merging

## Red Flags

If you're doing these, you're not orchestrating properly:

- Writing implementation code (that's agent work)
- Skipping TODO file creation (agents need specs)
- Vague task descriptions (agents will ask questions or guess)
- Assigning dependent tasks to parallel agents (conflicts)

## Example Session

```
User: "Add user authentication and a dashboard"

Opus thinks: Two features, can be parallel if auth is done first.

1. Plan both features
2. /assign-agent for auth → TODO-agent-auth.md
3. Push to main
4. Give user startup prompt for agent-auth
5. While agent-auth works, plan dashboard details
6. When auth PR merged, /assign-agent for dashboard
7. Review PRs, merge, update master TODO
```
