# Contributing to FlowLens

Thanks for taking a look. Bug reports, questions about a number, and pull requests are all welcome.

## Reporting a problem

Open an issue with the [bug report](https://github.com/singha105/flowlens/issues/new?template=bug_report.yml) form. The most useful reports name the repository you ran FlowLens on, the command, what you expected, and what you got. If a metric looks wrong, include an issue or PR number it should (or shouldn't) count: every number in FlowLens can be traced to the items behind it, so one example is usually enough to find the cause.

Please don't paste tokens or private repository data into an issue.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and, to ingest real data, the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`.

```bash
git clone https://github.com/singha105/flowlens && cd flowlens
uv sync
uv run pytest
```

The tests never touch the network (`tests/conftest.py` blocks sockets), so they run without a token.

## Making a change

1. Open an issue first for anything bigger than a fix, so we can agree on the approach.
2. Keep metric definitions in [docs/METRICS.md](docs/METRICS.md) in step with the code. A change to what a metric counts is a change to its definition, and it needs a sentence there.
3. Add tests. Metrics are tested against synthetic histories with known answers ([tests/synthetic.py](tests/synthetic.py)); ingest is tested against recorded API responses ([tests/fixtures/](tests/fixtures/)). New behaviour should come with a case that would fail without it.
4. Run the same checks CI runs:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   ```

5. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (`feat(metrics): ...`, `fix(ingest): ...`, `docs: ...`).

## Ground rules

- FlowLens reports on the system of work, not on individuals. Changes that rank or score developers won't be merged.
- No paid services or hosted dependencies: it has to stay free to run.
- Tokens are read from `gh auth token` at run time and never written to disk or to fixtures. Re-record fixtures with `scripts/record_fixtures.py`, and trim commit messages to their subject lines before committing them, as the existing fixtures are.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
