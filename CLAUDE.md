# Lectern — notes for Claude

A local AI workbench: a FastAPI app (`src/lectern`) serving a React/TypeScript UI (`web/`),
running models on the user's own GPU via llama.cpp or MLX. Single local user; the design is in
`docs/blueprint.html`, milestone acceptance criteria in `docs/acceptance/`.

## Commands

| What | Command |
|---|---|
| Install | `uv sync && npm --prefix web ci` |
| Test | `uv run pytest && npm --prefix web test` (one test: `uv run pytest <file> -k <name>`) |
| Lint | `uv run ruff check <file>`; web: `npm --prefix web run lint` (`--max-warnings 0`) |
| Format | `uv run ruff format <file>` (format only files you touch) |
| Build | `npm --prefix web run build` → `src/lectern/web_dist/` |
| Run | `uv run lectern serve` → `http://127.0.0.1:8765` |
| CI | `.github/workflows/ci.yml`, on pull requests and pushes to `main`: the lints, the web build, both test suites (Linux; `gpu`/`sandbox` tests skip) |

These run on the host, not in a container, on purpose: the app needs the GPU (Metal/MLX here,
CUDA on the Linux test machines), which a container can't reach on macOS. Host `python3` is
3.9; always go through `uv`, which provides 3.12 from `.python-version`. There is no
`timeout`/`gtimeout` on this Mac.

Tests marked `gpu` and `sandbox` need a real llama-server or container image (env vars in
`pyproject.toml`); without them they skip. pytest runs with `filterwarnings = error`.

<!-- toolkit:begin family-rules -->

## Python conventions

### Style

- **PEP 8**, 4 spaces, and whatever formatter the project already uses — match it rather than
  introducing a second one.
- **Type hints** on function signatures, including the return type. `Optional[X]` is explicit,
  never implied by a `None` default.
- f-strings for interpolation; never `%` or `.format()` in new code, and **never** an f-string
  to build SQL.
- Module-level names are `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`.
- Prefer specific exceptions over bare `except:` — a bare except swallows `KeyboardInterrupt`
  and every programming error along with the one you meant to handle.
- Context managers (`with`) for anything that must be closed.
- **Never use a mutable default argument** (`def f(items=[])`) — it is shared across calls and
  the resulting bug is reproducible only in sequence.

### Design priorities

Prefer the design that is easier to read, then easier to change, then easier to extend, then
cheaper in memory and time — in that order. Efficiency is last, not absent: weigh it at the scale
the data actually has, and give an optimisation that costs readability a measured reason.

### Code provenance

New code is written here, installed as a dependency through the package manager, or taken from
code under this project's own licence with its header kept. Code from a codebase under a
different licence — closed, or open source, MIT included — needs the developer's approval
first and its source and licence named in the commit; renaming a pasted block does not make it
original, and a library the project could depend on is not vendored piecemeal.

### Database

- **Parameterised queries only.** Pass parameters to the driver; never build SQL by
  concatenation or f-string, not even for an integer.
- Use the ORM or query layer the project already has, and iterate large result sets rather than
  loading them whole.

### Security

- **Never `eval()`, `exec()`, or `pickle.loads()` on anything a user can influence.** Pickle is
  arbitrary code execution by design — use JSON for untrusted data.
- **Never build a shell command from user input.** Use `subprocess` with an argument list and
  `shell=False`; if a shell is truly required, the input must be validated against an allowlist.
- **Secrets come from the environment or a secret store**, never from source. Confirm the ignore
  rules cover local env files, and that no credential reaches a log line.
- **Validate uploads by content**, store outside any served directory, and generate the stored
  filename rather than trusting the client's.
- Pin dependencies with hashes where the tooling supports it, and treat a known-vulnerable
  dependency as work rather than backlog.
- `requests` and friends: keep certificate verification on. `verify=False` in committed code is
  a finding.

### Web applications

- **Debug mode off in production.** A debug traceback page is remote code execution in some
  frameworks and an information leak in all of them.
- **CSRF protection on every state-changing route.** Enable the framework's protection rather
  than hand-rolling it.
- Session cookies `HttpOnly`, `Secure`, `SameSite`; rotate the session on login and on privilege
  change.
- **Templates escape by default in Jinja2 and Django — do not defeat it.** `|safe`,
  `Markup(...)` and `mark_safe(...)` disable escaping for that value; each use needs a reason
  that survives review.
- Authorisation is checked in the route handler, not only in the template that draws the link.

### Long-running work

Work the user need not wait for does not run on the path they wait on. In a web application that
path is the request: an export, a bulk recalculation, a call to an outside service — anything that
may outlast a page load — goes to the task queue or scheduled command the project already has
(Celery, RQ, a cron-driven management command); the request returns at once, and the user is told
when it is done. Reuse that mechanism rather than adding another. In a CLI the caller is the one
waiting: a progress indicator, or a lock on a resource that must not be used mid-change, is the
right tool there.

### Changing data in bulk

Any script that writes across many rows: **dry-run by default**, **safe to run twice**,
**bounded scope with an expected-row-count assertion** that aborts when reality disagrees, and a
stated recovery path before it runs.

### Tests

- `pytest` with fixtures in `conftest.py` for shared setup.
- Test the error paths, not just the happy one — bad input, missing record, failed write, empty
  result.
- No real clock, no unseeded randomness, no `sleep()` in a test; inject a clock, fix a seed,
  wait on a condition.

<!-- toolkit:end family-rules -->

## This project specifically

- **Security is a middleware, not per-route** (`src/lectern/core/security.py`): Host check
  (DNS rebinding), Origin check on mutating requests and every WebSocket, the per-install
  token on every `/api` request (`X-Lectern-Token`, or a WS subprotocol), and a strict CSP.
  That token check is the CSRF protection; don't add a second mechanism, and don't loosen the
  host/origin allowlist beyond `LECTERN_PUBLIC_URL`.
- **Authorisation:** every `/api` route depends on `current_user` (`core/auth.py`). It is a
  single local user for the MVP; keep the dependency on new routes so login can land later.
- **Escaping:** the UI is React, and Markdown goes through `react-markdown` without
  `rehype-raw`, so raw HTML is not rendered. Keep it that way; no `dangerouslySetInnerHTML`.
- **UI strings** go through i18next (`web/src/locales/`); eslint fails on literal strings.
- **Database:** SQLite, SQLAlchemy + Alembic, migrated in-process on start, in two trees. The
  app database in `LECTERN_HOME` (`core/tables.py`) → `src/lectern/migrations/`; each project's
  `<project>/.lectern/project.sqlite` (`projects/tables.py`) → `src/lectern/project_migrations/`.
  A schema change needs a new version in the tree that owns the table.
- **Imports are typed by extension** (`projects/text.py` `mime_for`), not by content. What keeps
  them safe is the serving (`api/projects.py`): a download unless the type is in `INLINE` (text,
  raster images), with `nosniff` and the CSP. A new inline type needs a content check first.
- **Generated, never hand-edit:** `src/lectern/web_dist/` (UI build, shipped in the wheel —
  without it `serve` answers 503), `dist/`, `uv.lock`/`web/package-lock.json` (via the tools).
- **Credentials:** the access token lives in the data dir (`~/.lectern/data` when installed),
  settings in `~/.lectern/lectern.env`; neither is in the repo. Never log the token.
- **Agent-written scripts** run sandboxed in Podman/Docker with only their project folder
  mounted; keep it that way.
- Local-only work notes: `.tasks/` (git-ignored); open work is under "Open now" in
  `.tasks/lectern-ui-followups.md`.
