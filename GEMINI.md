# Python Code Style

- All code must be type-hinted.
- All code must pass the project's linting rules.
  - DO NOT edit any linting rules from pyproject.toml. They are there for a reason. You must comply with them.
  - Use `uv run ruff check --fix` to check and fix the code.
  - Use `uv run ruff format` to format the code.
- Any data loaded from a file or external source (e.g. YAML, TOML, JSON, HTTP) must be validated against a Pydantic model. Never trust outside data. This includes data we may have written ourselves to a file - it may have been edited in between writing and reading.
- Data that is wholly internal to the application should be represented using standard Python classes or dataclasses. Pydantic validation is not necessary.
- Use Enums whereever possible. Do not create/pass around 'magic' strings or integers when there is a fixed set of values.
- Functions should never return bare dicts or tuples. Create and use NamedTuples, dataclasses or Python classes whereever possible.
  - NamedTuples are simpler than Dataclasses, which are simpler than Python classes - prefer simpler whereever possible.
  - Strongly prefer to make dataclasses immutable where possible. Use @dataclass(frozen=True)
  - It's OK to use dicts/tuples strictly within the scope of a single function. In this case leave a comment describing the data structure.
- Do not leave comments as questions to yourself in the code. Either figure it out or ask me.
- Do not leave comments in the code that are not necessary for understanding the code.
   - The exception is in test code. Copious comments explaining the 'why' are allowed in test code.
- Do not 'number' steps in the code. It's not necessary.

# Running Tests

- Use `uv run pytest` to run tests.

# Committing Code

- You must never commit code without ensuring the ruff checks above pass, and the code passes all the tests. 
- You must always use `git add` to stage files before committing. You should never use `git commit -a`.


# Time and Date

- All time and date usage and calculations MUST be timezone aware. Never create datetimes or similar without explicitly specifying a timezone.

# File Manipulation

- Always tell git what you are doing. For example, when moving a file, always use 'git mv', never bare 'mv'. Also 'git rm' etc.

# Conventional Commits

- Commit messages follow the (Conventional Commits)[https://www.conventionalcommits.org/en/v1.0.0/#summary] spec.

# Localisation

- You write in Australian English. All spelling, grammar, idioms and style should reflect this. This applies to documentation, commit messages, code comments, variables, API names etc.
