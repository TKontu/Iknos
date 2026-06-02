# Assign Agent

Create a complete task assignment for a Sonnet executor agent.

## Context
```bash
cat docs/TODO*.md 2>/dev/null | head -100
```
```bash
git log --oneline -5
```
```bash
ls .claude/templates/
```

## Instructions

### 1. Identify Agent and Task

- Choose agent ID: `agent-{descriptive-name}` (e.g., `agent-auth`, `agent-api`)
- Define the task scope - must be independently completable

### 2. Create TODO File

Read `.claude/templates/TODO-agent-template.md` and create `docs/TODO-{agent-id}.md`:

**Required sections:**
- **Context** - What the agent needs to know (no implicit knowledge)
- **Objective** - Single clear sentence
- **Tasks** - Numbered, with file paths and test cases
- **Constraints** - What NOT to do
- **Verification** - How to confirm it's done

**Quality checklist before creating:**
- [ ] Can agent complete this without asking questions?
- [ ] Are file paths explicit?
- [ ] Are test cases specified?
- [ ] Are edge cases mentioned?
- [ ] Would YOU know what to build from this spec?

### 3. Commit and Push

```bash
git add docs/TODO-{agent-id}.md
git commit -m "docs: assign {task-description} to {agent-id}"
git push origin main
```

### 4. Output Agent Setup Instructions

**If agent folder doesn't exist yet:**

```markdown
## Setup Agent Workspace: {agent-id}

### One-Time Setup
```bash
# Create agent folder (adjust path as needed)
cp -r /mnt/c/code/knowledge_extraction /mnt/c/code/knowledge_extraction-{agent-id}

# Replace CLAUDE.md with executor version
cp /mnt/c/code/knowledge_extraction/.claude/templates/CLAUDE-executor.md \
   /mnt/c/code/knowledge_extraction-{agent-id}/CLAUDE.md

# Navigate to agent folder
cd /mnt/c/code/knowledge_extraction-{agent-id}
```

### Start Agent Session
Open new Claude Code session in the agent folder.

**Startup prompt:**
```
I am executor agent {agent-id}.
git pull origin main
Read docs/TODO-{agent-id}.md
Execute tasks using TDD. Create PR when done.
```
```

**If agent folder already exists:**

```markdown
## Start Agent: {agent-id}

In agent workspace `/mnt/c/code/knowledge_extraction-{agent-id}`:

**Startup prompt:**
```
I am executor agent {agent-id}.
git pull origin main
Read docs/TODO-{agent-id}.md
Execute tasks using TDD. Create PR when done.
```
```

## Agent Naming Convention

| ID | Use For |
|----|---------|
| `agent-auth` | Authentication features |
| `agent-api` | API endpoints |
| `agent-models` | Database models |
| `agent-extract` | Extraction logic |
| `agent-tests` | Test coverage |
| `agent-1`, `agent-2` | Generic when no clear domain |

## Common Mistakes

- **Vague specs**: "Add validation" → "Add email format validation returning error 'Invalid email format' for non-matching inputs"
- **Missing files**: "Update the service" → "Update `src/services/extraction.py` function `extract_entities`"
- **No test cases**: Always specify what tests to write
- **Implicit knowledge**: Agent doesn't know what you know. Spell it out.
