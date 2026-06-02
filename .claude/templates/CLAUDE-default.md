# CLAUDE.md - Python Project Guidelines

## Common Commands

```bash
# Environment setup
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\activate     # Windows

# Dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install -e .  # Editable install

# Testing
pytest                          # Run all tests
pytest tests/test_foo.py -v     # Single file, verbose
pytest -k "test_name"           # Run matching tests
pytest --cov=src --cov-report=html  # Coverage report

# Linting & Formatting
ruff check .                    # Lint
ruff check . --fix              # Lint + autofix
ruff format .                   # Format code
mypy src/                       # Type checking

# Running (FastAPI)
uvicorn app.main:app --reload              # Dev server
uvicorn app.main:app --host 0.0.0.0 --port 8000  # Production
```

## Ruff Configuration

```toml
# pyproject.toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ASYNC"]
ignore = ["E501"]  # Line length handled by formatter

[tool.ruff.lint.isort]
known-first-party = ["app"]
```

## Code Style Conventions

### General Rules
- Follow PEP 8, enforced via Ruff
- Max line length: 88 characters
- Type hints required for all function signatures
- Prefer explicit imports over wildcard imports

### Naming
- `snake_case`: functions, variables, modules
- `PascalCase`: classes, type aliases
- `SCREAMING_SNAKE_CASE`: constants
- `_private`: internal use (single underscore)

### Imports
Order: stdlib → third-party → local, separated by blank lines:
```python
import os
from pathlib import Path

import httpx
from pydantic import BaseModel

from app.core import config
from app.models import User
```

### Docstrings
Use Google-style docstrings:
```python
def process_data(items: list[str], limit: int = 10) -> dict[str, int]:
    """Process items and return frequency counts.

    Args:
        items: List of strings to process.
        limit: Maximum items to return.

    Returns:
        Dictionary mapping items to their counts.

    Raises:
        ValueError: If items is empty.
    """
```

### Type Hints
```python
# Use modern syntax (Python 3.10+)
def fetch(ids: list[int]) -> dict[str, Any] | None: ...

# For complex types, use TypeAlias
type UserMap = dict[str, list[User]]
```

## FastAPI Patterns

### Router Structure
```python
# app/api/routes/users.py
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, service: UserService = Depends(get_user_service)) -> User:
    if not (user := await service.get(user_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user
```

### Exception Handlers
```python
# app/main.py
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
```

### Lifespan Events
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await database.connect()
    yield
    # Shutdown
    await database.disconnect()

app = FastAPI(lifespan=lifespan)
```

## State Management

### Dependency Injection
Use constructor injection or FastAPI's `Depends`:
```python
class UserService:
    def __init__(self, db: Database, cache: Cache) -> None:
        self._db = db
        self._cache = cache

# FastAPI pattern
def get_user_service(db: Database = Depends(get_db)) -> UserService:
    return UserService(db)
```

### Configuration
- Use pydantic-settings for config with env vars
- Never hardcode secrets; use environment variables
- Config hierarchy: defaults < env vars < explicit args

## Logging

```python
import structlog

logger = structlog.get_logger(__name__)

# Structured logging with context
logger.info("user_created", user_id=user.id, email=user.email)
logger.error("payment_failed", order_id=order.id, reason=str(e))
```

### Log Levels
- `DEBUG`: Detailed diagnostic info
- `INFO`: General operational events
- `WARNING`: Unexpected but handled situations
- `ERROR`: Failures requiring attention
- `CRITICAL`: System-level failures

## Error Handling

### Exception Hierarchy
```python
class AppError(Exception):
    """Base exception for application errors."""
    def __init__(self, message: str, code: str = "INTERNAL_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)

class ValidationError(AppError): ...
class NotFoundError(AppError): ...
class AuthenticationError(AppError): ...
```

### Best Practices
- Never use bare `except:`; catch specific exceptions
- Use `raise ... from e` to preserve exception chains
- Log exceptions with context before re-raising
- Convert external exceptions to domain exceptions at boundaries

```python
try:
    result = external_api.call()
except httpx.HTTPError as e:
    logger.error("api_call_failed", url=e.request.url, status=e.response.status_code)
    raise ServiceUnavailableError("External service failed") from e
```

## Feature Gating

```python
from app.core.config import settings

# Environment-based flags
if settings.feature_new_checkout_enabled:
    return new_checkout_flow(cart)
return legacy_checkout(cart)

# Percentage rollout
def is_feature_enabled(user_id: str, rollout_pct: int) -> bool:
    return hash(user_id) % 100 < rollout_pct
```

## Pytest Patterns

### Fixtures (conftest.py)
```python
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest.fixture
def user_factory() -> Callable[..., User]:
    def _create(**overrides) -> User:
        defaults = {"name": "Test User", "email": "test@example.com"}
        return User(**(defaults | overrides))
    return _create
```

### Test Structure
```python
# tests/test_users.py
import pytest

class TestGetUser:
    async def test_returns_user_when_exists(self, client: AsyncClient) -> None:
        response = await client.get("/users/1")
        assert response.status_code == 200
        assert response.json()["id"] == 1

    async def test_returns_404_when_not_found(self, client: AsyncClient) -> None:
        response = await client.get("/users/999")
        assert response.status_code == 404
```

### Pytest Configuration
```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra -q"
```

## Debugging

- `breakpoint()` drops into pdb
- `pytest --pdb` debugs on test failure
- `pytest -x` stops on first failure
- `pytest --lf` runs last failed tests

## Pull Request Template

```markdown
## Summary
Brief description of changes and motivation.

## Changes
- Added X to handle Y
- Refactored Z for clarity
- Fixed bug where A caused B

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests pass
- [ ] Manual testing completed

## Checklist
- [ ] Type hints added
- [ ] Docstrings updated
- [ ] No new linter warnings
- [ ] Breaking changes documented

## Related Issues
Closes #123
```
