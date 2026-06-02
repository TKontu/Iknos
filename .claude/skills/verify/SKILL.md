---
name: verify
description: Use before claiming work is complete - run verification commands and confirm output
---

# Verification Before Completion

## The Rule

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

## Before Saying "Done"

1. **Run the tests**
   ```bash
   pytest
   ```
   Must see: All pass, 0 failures

2. **Run the linter**
   ```bash
   ruff check .
   ```
   Must see: No errors

3. **Check types (if applicable)**
   ```bash
   mypy src/
   ```
   Must see: Success

## Evidence Format

When reporting completion:

```
Verification:
- pytest: 47/47 passed
- ruff check: 0 errors
- mypy: Success
```

## Red Flags

If you're thinking:
- "Should work now" - RUN IT
- "I'm confident" - RUN IT
- "Just this once" - NO, RUN IT

## Never

- Claim tests pass without running them
- Say "should work" instead of verifying
- Skip verification because you're confident
- Trust previous runs - run fresh

## Checklist

Before PR/commit:
- [ ] pytest run, output shows all pass
- [ ] ruff check run, output shows clean
- [ ] No errors or warnings
- [ ] Actually read the output, don't just run
