# Releasing mini-lean-agent

## Before a release

1. Confirm the exact mini-SWE-agent pin in `pyproject.toml` against the upstream public API.
2. Update the package version in `pyproject.toml`, `src/minileanagent/__init__.py`, and
   `CITATION.cff`.
3. Update `CHANGELOG.md`.
4. Run:

   ```bash
   uv sync --extra dev
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest --cov=minileanagent
   uv build
   uv run twine check dist/*
   ```

5. Tag the release with the matching version, for example `v0.2.0`.
6. Publish a GitHub Release from that tag.

The release workflow checks the tag, rebuilds the package, attaches distributions to the GitHub
Release, and publishes to PyPI with trusted publishing.

## One-time PyPI configuration

Create a trusted publisher with:

- owner: `TomasOrtega`
- repository: `mini-lean-agent`
- workflow: `publish.yml`
- environment: `pypi`

Create the matching `pypi` GitHub environment. No long-lived PyPI token is required.
