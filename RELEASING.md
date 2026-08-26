# Releasing mini-lean-agent

This repository is prepared for GitHub Pages, GitHub Releases, and PyPI trusted publishing.

## One-time repository setup

1. From the committed local checkout, create the public repository and push `main`:

   ```bash
   gh repo create TomasOrtega/mini-lean-agent \
     --public \
     --source=. \
     --remote=origin \
     --push \
     --description "A minimal, reproducible agent baseline for Lean theorem-proving tasks"
   git push origin v0.1.0
   ```

   The second command publishes the annotated release tag included in the initial repository bundle.
2. In **Settings → Pages**, select the `gh-pages` branch as the Pages source after the documentation workflow creates it.
3. On PyPI, create a trusted publisher for:
   - owner: `TomasOrtega`
   - repository: `mini-lean-agent`
   - workflow: `publish.yml`
   - environment: `pypi`
4. In GitHub **Settings → Environments**, create the `pypi` environment. Requiring a reviewer is recommended.
5. Confirm that CI and documentation workflows pass on `main`.

No long-lived PyPI token is required.

## Release checklist

1. Update `__version__` in `src/minileanagent/__init__.py` and `version` in `pyproject.toml`.
2. Update `CHANGELOG.md` and `CITATION.cff`.
3. Run:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest --cov=minileanagent
   uv build
   uv run twine check dist/*
   ```

4. Commit the release and create a tag matching the package version, for example `v0.1.0`.
5. Create and publish a GitHub Release from that tag.

Publishing the GitHub Release triggers `.github/workflows/publish.yml`. The workflow verifies that the tag matches the package version, rebuilds and checks the distributions, attaches them to the GitHub Release, and publishes them to PyPI through trusted publishing.
