# TODO: {Task Name}

**Agent:** {agent-id}
**Branch:** `feat/{branch-name}`
**Priority:** {high|medium|low}

## Context

{Brief context about where this fits in the larger project. What has already been done. What the agent needs to know.}

## Objective

{Single clear sentence describing what this agent must accomplish.}

## Tasks

### 1. {First Task}

**File(s):** `src/path/to/file.py`

**Requirements:**
- {Specific requirement 1}
- {Specific requirement 2}

### 2. {Second Task}

**File(s):** `src/path/to/other.py`

**Requirements:**
- {Specific requirement}

## Constraints

- {Any limitations or things NOT to do}
- {Dependencies to be aware of}
- Do NOT run full test suite - only run tests in Test Scope below
- Do NOT lint entire codebase - only lint files in Lint Scope below

## Test Scope

**ONLY run these tests - do NOT run `pytest` without arguments:**

```bash
pytest tests/test_{feature_name}.py -v
```

## Lint Scope

**ONLY lint these files - do NOT run `ruff check src/`:**

```bash
ruff check src/path/to/new_file.py src/path/to/modified_file.py
```

## Verification

Before creating PR, run ONLY the scoped commands above:

1. `pytest tests/test_{feature}.py -v` - Must pass
2. `ruff check {your files}` - Must be clean
3. All tasks above completed

## Definition of Done

- [ ] All tasks completed
- [ ] Tests written and passing (scoped)
- [ ] Lint clean (scoped)
- [ ] PR created with title: `feat: {description}`
