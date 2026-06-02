# New Endpoint

Scaffold a new FastAPI endpoint. Argument: endpoint description (e.g., "GET /users/{id}").

## Context

```bash
find . -name "*.py" -path "*/routes/*" | head -5
```

```bash
find . -name "*.py" -path "*/schemas/*" | head -5
```

## Instructions

Based on the argument, create:

1. **Route handler** in appropriate routes file:
   - Use existing router patterns from context
   - Include proper type hints and response_model
   - Add HTTPException for error cases

2. **Pydantic schemas** if needed:
   - Request model (if POST/PUT/PATCH)
   - Response model

3. **Test file** with:
   - Happy path test
   - Error case test (404, 422, etc.)

Follow existing project patterns for naming and structure.
