# Secrets Check

Scan staged files for exposed secrets before commit.

## Context

```bash
git diff --cached --name-only
```

## Instructions

Scan the staged files for common secret patterns:

### Patterns to detect:
- API keys: `api[_-]?key`, `apikey`, `api[_-]?secret`
- AWS: `AKIA[0-9A-Z]{16}`, `aws_secret_access_key`
- Tokens: `token`, `bearer`, `auth[_-]?token`
- Passwords: `password\s*=`, `passwd`, `pwd`
- Private keys: `-----BEGIN.*PRIVATE KEY-----`
- Database URLs: `postgres://`, `mysql://`, `mongodb://` with credentials
- Generic secrets: `secret[_-]?key`, `client[_-]?secret`

### For each staged file:

```bash
git show :$FILE | grep -iE "(api[_-]?key|secret|password|token|bearer|AKIA|BEGIN.*PRIVATE)" || true
```

## Output

If secrets found:
```
🔴 SECRETS DETECTED - Do not commit!

file.py:23 - Possible API key: api_key = "sk-..."
config.py:5 - Hardcoded password: password = "..."

Recommended:
1. Move secrets to .env
2. Use environment variables
3. Add file to .gitignore if needed
```

If clean:
```
✅ No secrets detected in staged files
```
