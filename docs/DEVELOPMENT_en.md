<div align="center">

[中文](DEVELOPMENT.md) | [English](DEVELOPMENT_en.md) | [Русский](DEVELOPMENT_ru.md)

</div>

# LivingMemory Developer Guide

**Version**: v2.0.0
**Last Updated**: 2025-12-17

---

## Table of Contents

1. [Development Environment Setup](#development-environment-setup)
2. [Project Structure](#project-structure)
3. [Development Workflow](#development-workflow)
4. [Testing Guide](#testing-guide)
5. [Code Standards](#code-standards)
6. [Debugging Tips](#debugging-tips)
7. [Contribution Guide](#contribution-guide)

---

## Development Environment Setup

### Prerequisites

- Python 3.10+
- AstrBot development environment
- Git

### Install Dependencies

```bash
# Clone repository
git clone https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory.git
cd astrbot_plugin_livingmemory

# Install dependencies
pip install -r requirements.txt

# Install development dependencies
pip install pytest pytest-asyncio pytest-cov
```

### IDE Configuration

Recommended: VSCode or PyCharm.

**VSCode Configuration** (`.vscode/settings.json`):
```json
{
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": true,
  "python.formatting.provider": "black",
  "python.testing.pytestEnabled": true,
  "python.testing.unittestEnabled": false
}
```

---

## Project Structure

```
astrbot_plugin_livingmemory/
├── main.py                          # Plugin main file
├── core/                            # Core modules
│   ├── exceptions.py                # Exception definitions
│   ├── config_manager.py            # Configuration management
│   ├── plugin_initializer.py       # Plugin initialization
│   ├── event_handler.py            # Event handling
│   ├── command_handler.py          # Command handling
│   ├── tools/                      # Agent/LLM tools
│   ├── memory_engine.py            # Memory engine
│   ├── memory_processor.py         # Memory processing
│   ├── conversation_manager.py     # Conversation management
│   └── ...
├── storage/                        # Storage layer
│   ├── conversation_store.py       # Conversation storage
│   └── db_migration.py             # Database migration
├── webui/                          # Web management interface
│   └── server.py                   # FastAPI server
├── tests/                          # Test suite
│   ├── conftest.py                 # pytest configuration
│   ├── test_*.py                   # Unit tests
│   ├── integration/                # Integration tests
│   └── performance_test.py         # Performance test
├── docs/                           # Documentation
│   ├── API.md                      # API documentation
│   └── DEVELOPMENT.md              # Developer guide
```

### Module Responsibilities

| Module | Responsibility | Dependencies |
|------|------|------|
| main.py | Plugin registration and lifecycle | All core modules |
| exceptions.py | Exception definitions | None |
| config_manager.py | Configuration management | config_validator |
| plugin_initializer.py | Plugin initialization | All core modules |
| event_handler.py | Event handling | memory_engine, conversation_manager |
| command_handler.py | Command handling | memory_engine, conversation_manager |
| tools/ | Agent tools encapsulation | memory_engine, config_manager |

---

## Development Workflow

### 1. Create a New Feature

```bash
# Create a new branch
git checkout -b feature/your-feature-name

# Develop feature
# ...

# Run tests
pytest tests/

# Commit code
git add .
git commit -m "feat: add your feature"
git push origin feature/your-feature-name
```

### 2. Fix a Bug

```bash
# Create a fix branch
git checkout -b fix/bug-description

# Fix bug
# ...

# Add tests
# ...

# Run tests
pytest tests/

# Commit code
git add .
git commit -m "fix: fix bug description"
git push origin fix/bug-description
```

### 3. Refactor Code

```bash
# Create a refactor branch
git checkout -b refactor/what-to-refactor

# Refactor code
# ...

# Ensure all tests pass
pytest tests/

# Commit code
git add .
git commit -m "refactor: refactor description"
git push origin refactor/what-to-refactor
```

---

## Testing Guide

### Running Tests

```bash
# Run all tests
pytest tests/

# Run a specific test file
pytest tests/test_config_manager.py

# Run a specific test function
pytest tests/test_config_manager.py::test_config_manager_initialization

# View coverage
pytest --cov=core tests/

# Generate HTML coverage report
pytest --cov=core --cov-report=html tests/
```

### Writing Tests

#### Unit Test Example

```python
import pytest
from core.config_manager import ConfigManager

def test_config_manager_get():
    """Test configuration retrieval"""
    config = ConfigManager({"key": "value"})
    assert config.get("key") == "value"
    assert config.get("non_existent", "default") == "default"

@pytest.mark.asyncio
async def test_async_function():
    """Test async function"""
    result = await some_async_function()
    assert result is not None
```

#### Mock Usage Example

```python
from unittest.mock import Mock, AsyncMock, patch

def test_with_mock():
    """Test using mock"""
    mock_obj = Mock()
    mock_obj.method = Mock(return_value="test")

    result = mock_obj.method()
    assert result == "test"
    assert mock_obj.method.called

@pytest.mark.asyncio
async def test_with_async_mock():
    """Test using async mock"""
    mock_obj = Mock()
    mock_obj.async_method = AsyncMock(return_value="test")

    result = await mock_obj.async_method()
    assert result == "test"
```

### Performance Testing

```bash
# Run performance tests
python3 tests/performance_test.py
```

---

## Code Standards

### Python Code Style

Follow PEP 8:

```python
# Good example
def calculate_score(
    importance: float,
    recency: float,
    weight: float = 1.0
) -> float:
    """
    Calculate final score

    Args:
        importance: Importance
        recency: Timeliness
        weight: Weight

    Returns:
        Final score
    """
    return importance * recency * weight


# Avoid example
def calc(i,r,w=1.0):  # Unclear naming, missing type annotations
    return i*r*w  # Missing docstring
```

### Naming Conventions

- **Class names**: PascalCase (e.g., `ConfigManager`)
- **Function names**: snake_case (e.g., `get_config`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_RETRIES`)
- **Private methods**: underscore prefix (e.g., `_internal_method`)

### Docstrings

Use Google-style docstrings:

```python
def process_memory(
    content: str,
    metadata: dict,
    importance: float = 0.5
) -> tuple[str, dict, float]:
    """
    Process memory content

    Args:
        content: Memory content
        metadata: Metadata dictionary
        importance: Importance score

    Returns:
        tuple: (Processed content, updated metadata, final importance)

    Raises:
        MemoryProcessingError: Thrown when processing fails

    Example:
        >>> content, meta, score = process_memory("test", {}, 0.8)
        >>> print(score)
        0.8
    """
    # Implementation...
    pass
```

### Type Annotations

All public functions should have complete type annotations:

```python
from typing import Any, Optional, List, Dict

def get_memories(
    session_id: str,
    limit: int = 10,
    filters: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Get memory list"""
    pass
```

---

## Debugging Tips

### Using Logs

```python
from astrbot.api import logger

# Different log levels
logger.debug("Debug information")
logger.info("General information")
logger.warning("Warning information")
logger.error("Error information", exc_info=True)  # Include stack trace
```

### Using Breakpoints

```python
# Add breakpoint in code
import pdb; pdb.set_trace()

# Or use breakpoint() (Python 3.7+)
breakpoint()
```

### Inspecting Variables

```python
# Print variable
print(f"Variable value: {variable}")

# Use logger
logger.debug(f"Variable value: {variable}")

# Use pprint for formatted output
from pprint import pprint
pprint(complex_dict)
```

### Performance Profiling

```python
import time

start_time = time.time()
# Execute operation
end_time = time.time()
logger.info(f"Operation time: {end_time - start_time:.4f} seconds")
```

---

## Contribution Guide

### Commit Convention

Use Conventional Commits:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation update
- `style`: Code formatting (does not affect functionality)
- `refactor`: Refactoring
- `test`: Test-related
- `chore`: Build/tools related

**Examples**:
```
feat(memory): add memory importance decay

Implemented time-based memory importance decay mechanism using exponential decay function.

Closes #123
```

### Pull Request Process

1. Fork the repository
2. Create a feature branch
3. Write code and tests
4. Ensure all tests pass
5. Submit a Pull Request
6. Wait for code review
7. Revise based on feedback
8. Merge into the main branch

### Code Review Checklist

- [ ] Code follows standards
- [ ] Has complete type annotations
- [ ] Has docstrings
- [ ] Has unit tests
- [ ] All tests pass
- [ ] Has not introduced new dependencies (or has explained them)
- [ ] Updated relevant documentation
- [ ] Commit message is clear

---

## Frequently Asked Questions

### Q: How to add a new configuration item?

A:
1. Add configuration definition in `_conf_schema.json`
2. Add validation logic in `config_validator.py`
3. Add access method in `ConfigManager` if needed
4. Update documentation

### Q: How to add a new command?

A:
1. Add handling method in `CommandHandler`
2. Add command decorator in `main.py`
3. Add unit tests
4. Update help documentation

### Q: How to debug initialization issues?

A:
1. Check log output
2. Use `initialization_status_callback` to get status
3. Check if Provider is ready
4. Check `_initialization_error` property

---

## Resource Links

- [API Documentation](API.md)
- [Architecture Documentation](ARCHITECTURE.md)
- [GitHub Repository](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
- [Issue Feedback](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues)

---

**Document Version**: v2.0.0
**Last Updated**: 2025-12-17
