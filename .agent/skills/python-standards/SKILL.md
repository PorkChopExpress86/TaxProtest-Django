---
name: python-standards
description: Authoritative guide for Python coding standards, strict type hinting, and modern 3.11+ syntax. triggers when writing, refactoring, or reviewing Python code.
---

# GOAL
Produce production-grade, secure, and type-safe Python code that strictly adheres to PEP 8 and modern conventions (Python 3.11+).

# INSTRUCTIONS
1.  **Type Hinting:** - MANDATORY for all function signatures and class attributes.
    - Use modern syntax: `str | None` instead of `Optional[str]`, `list[int]` instead of `List[int]`.
    - Use `typing.Self` for methods returning the instance.
2.  **Path Handling:**
    - NEVER use `os.path.join`. ALWAYS use `pathlib.Path`.
3.  **Structure:**
    - Use `pydantic.BaseModel` or `@dataclass` for data structures; avoid raw dictionaries.
    - Use `if __name__ == "__main__":` blocks for script execution.
4.  **Error Handling:**
    - Fail fast. Validate inputs immediately.
    - Create custom exception classes for domain-specific errors.

# CONSTRAINTS
- Do not use `pass` placeholders; write full implementation.
- Do not use `print()` for logging; use the `logging` module or `structlog`.
- Do not use global variables.

# EXAMPLES
<example>
Input: "Write a function to read a text file."
Output:
```python
from pathlib import Path

def read_file_content(file_path: Path) -> str:
    """Reads text from a path safely."""
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    with file_path.open("r", encoding="utf-8") as f:
        return f.read()