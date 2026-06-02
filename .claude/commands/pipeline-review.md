# Pipeline Review

Review an endpoint's pipeline end-to-end. Argument: endpoint path (e.g., "/users/{id}" or "POST /orders").

## Context

```bash
# Find route files
find . -name "*.py" -path "*/routes/*" -o -name "*.py" -path "*/api/*" | head -10
```

```bash
# Find service files
find . -name "*.py" -path "*/services/*" -o -name "*service*.py" | head -10
```

```bash
# Find dependency patterns
grep -r "Depends(" --include="*.py" -l | head -10
```

```bash
# Project structure
find . -type f -name "*.py" | grep -E "(route|service|repo|model|schema)" | head -20
```

## Instructions

Trace the full request lifecycle through every layer:

### 1. Route Layer
- Find the route handler matching the endpoint
- Review path/query parameters, request body validation
- Check authentication/authorization dependencies

### 2. Dependencies
- Map all `Depends()` injections
- Trace each dependency's initialization
- Identify shared state, connections, caches

### 3. Service Layer
- Follow business logic step-by-step
- Review input validation and transformations
- Check error handling and edge cases

### 4. Data Layer
- Review database queries/ORM operations
- Check external API calls, timeouts, retries
- Verify transaction boundaries

### 5. Response
- Validate response model matches actual return
- Check error responses and status codes

## For Each Step, Identify:

- **🔴 Errors**: Will cause failures (exceptions, missing handling, type mismatches)
- **🟠 Bugs**: Incorrect behavior (wrong logic, race conditions, data loss)
- **🟡 Issues**: Problems under certain conditions (edge cases, performance)

## Output

Create `endpoint_{name}_review.md`:

```markdown
# Pipeline Review: {endpoint}

## Flow
route.py:fn → dependency.py:dep → service.py:method → repo.py:query

## Critical (must fix)
- [ ] path/file.py:123 - Issue description

## Important (should fix)
- [ ] path/file.py:456 - Issue description

## Minor
- [ ] path/file.py:789 - Issue description
```

Keep it concise. Only document actual findings, not "all looks good" items.
