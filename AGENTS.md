# AGENTS.md

`orq` is a Python 3.11 CLI that inspects a *target* project and writes an OpenCode V2 agent
team into it: `.opencode/agents/*.md`, a `default_agent` patch in `opencode.jsonc`, and
`.orquestador.yaml`. It is not an OpenCode plugin and needs no OpenCode at runtime.

## Commands

- `uv sync` — the only dependency entrypoint. `src/` layout + hatchling means
  `import orquestador` works inside the uv env, not from a bare `python`.
- `uv run pytest` — full suite (~0.6s, 181 tests). One test:
  `uv run pytest tests/test_generate.py::test_force_never_clobbers_existing_config`.
  `tests/conftest.py` points `ORQ_MODELS_CACHE` at a missing file: tests must not depend on
  the machine's models.dev cache. Use the `models_cache` fixture to inject fake metadata.
- `uv run ruff check .` — correctness rules only (`E4/E7/E9/F`); `line-length = 100` is set
  for formatting, not enforced as E501.
- `uv run mypy` — typed over `src/orquestador` (`check_untyped_defs`). Both run in CI
  (`.github/workflows/ci.yml`) on Python 3.11 and 3.12; keep the baseline green.
- The global `orq` is installed editable (`uv tool install -e .`), so `src/` edits are live
  without reinstalling. New `[project].dependencies` are the exception: re-run
  `uv tool install -e .` to refresh the tool env.

## Layout (`src/orquestador`)

- `scan.py` — `ProjectProfile`: language counts, `runtimes`, evidence-based `frameworks`,
  test/lint/build commands read from scripts/manifests, secret files, key dirs. Caps at 4000
  files.
- `catalog.py` — merges project + `~/.config/opencode` configs, probes OpenAI-compatible
  `/v1/models`, reads OpenCode's models.dev cache (`~/.cache/opencode/models.json`) for
  `tool_call`, context, price and variants, assigns a tier.
- `planner.py` — `Strategy` (from `.orquestador.yaml`) → `auto_strategy` → `build_plan`;
  includes hierarchy presets and `CustomAgent` validation/resolution.
- `templates.py` — agent markdown, YAML frontmatter, prompts (one renderer per fixed agent
  plus a safe generic renderer for custom agents).
- `writer.py` — `planned_files`, JSONC-safe `default_agent` patch, agent/strategy writes,
  `validate()`.
- `fsutil.py` — `atomic_write_text` and `backup_file`; every generated file goes through it.
- `cli.py` — Typer commands and all console output; `cost.py` — cost estimates.

## Invariants tests enforce

- `opencode.jsonc` is **edited textually, never re-serialized**: `patched_config_text` parses
  it with `strip_jsonc` only to validate, then inserts or replaces `default_agent` in the raw
  text so comments, providers and MCP keys survive byte-for-byte. `patch_default_agent`
  returns `True` when it wrote, `None` when the value was already set and `False` only when
  the file is not valid JSONC (untouched). A named regression test covers comments; do not
  "simplify" this back to `json.dumps`.
- Every write goes through `fsutil.atomic_write_text` (temp file in the same directory +
  `os.replace`), so a crash cannot leave a half-written agent or config. `--backup` copies the
  previous version to `<name>.bak` before overwriting; it is opt-in and never creates backups
  when the file does not exist.
- `orq diff` renders the same `planned_files` as `write_plan` without touching disk, prints a
  unified diff and exits `1` when something would change (`0` when up to date). Keep both
  paths backed by `planned_files` so diff cannot drift from generate.
- `--force` rewrites only agent `.md` files. A named regression test covers this — do not
  "simplify" it away.
- Permissions serialize as `permissions: [{action, resource, effect}]` and evaluation is
  *last rule wins* (`effective_permissions`). Tests assert the literal
  `  - action: edit` / `    resource: "*"` / `    effect: deny` shape: keep indentation and
  field order.
- The orchestrator frontmatter must deny `external_directory`, `edit`, `shell` and
  `subagent: "*"`, and allow each delegated agent by name; `validate()` reports violations
  as `error`. `write` is not a V2 action (`edit` covers edit/write/patch): do not emit or
  require it. `default_agent` goes in `opencode.jsonc`, never in an agent file.
- Every agent denies `external_directory: "*"` and `read`/`edit` on `profile.secret_paths`.
  `local` also denies network shell patterns. Keep this policy when adding a renderer.
- `subagents.*.enabled: false` drops that agent from the plan (and its delegation allow-list);
  `generate` never deletes an existing `.md` for it, it only warns. Disabling is a plan-level
  decision, not a destructive clean-up. `enabled` accepts `true/false` and the string forms
  `"false"`/`"no"`/`"off"`/`"0"`.
- `preset` is `balanced` (default), `lean` or `strict`; `auto`/`default`/empty map to
  `balanced`. `lean` keeps exactly one worker, chosen by `local` > `barato` > `especialista`
  among enabled roles, and omits custom agents. Apply lean at plan time; never mutate the
  saved `enabled` flags, so switching back to `balanced` restores the full team.
- `validate(planned_names=...)` treats known generated files outside the plan (lean, disabled)
  as stale warnings, not as "orchestrator cannot delegate" errors. `generate` passes
  `report_stale=False` because `planned_files` already prints those notes.
- `preset: strict` denies `shell` on every subagent, turns cloud edits into `ask`
  (`barato`, `especialista`, custom `edit`/`full`) and denies `webfetch`/`websearch` to the
  orchestrator. The local agent keeps `edit: allow` (it is the private worker).
- Custom agents live under `subagents.<name>` (not `local`/`barato`/`especialista`), match
  `^[a-z][a-z0-9_-]{0,39}$`, are unique after lowercasing and cannot use reserved names.
  Fields: `model`, `steps`, `enabled`, `tier` (`local|cheap|frontier`), `permissions`
  (`read|edit|full`), `description`, `prompt`, `color` (`#rrggbb`). Invalid values raise
  `StrategyError` (CLI exit 2). `model: auto` resolves by tier; custom agents are routed and
  added to the orchestrator delegation allow-list, with the same external/secret/subagent
  denials as fixed agents. An explicit `tier` is never silently swapped: `tier: local` with
  no local model skips the agent instead of sending it to the cloud. Only tierless auto may
  fall back `cheap` → `frontier` → `local`.
- `scan` keeps `runtimes` and `frameworks` separate: `node`/`python`/`php` are runtimes, not
  frameworks. A framework needs evidence in the manifest (`laravel/framework`, `next`,
  `fastapi`, `gin`...); a bare `composer.json` is not Laravel. Docker and CI are `signals`,
  never frameworks.
- Commands come from the project before heuristics: `composer.json` scripts, `package.json`
  scripts and lockfile (npm/pnpm/yarn/bun), `pyproject.toml`/`requirements.txt` (uv + pytest +
  ruff), `Cargo.toml`, `go.mod`, `Gemfile`, Gradle/Maven, `.csproj`, `Package.swift` and
  `mix.exs`. `Makefile` targets (`test`/`lint`/`build`) are the last resort. Do not emit a
  command for a script that does not exist.
- Model refs are `provider/model` with optional `#variant`; helpers strip the variant when
  resolving. `Catalog.find` accepts the full ref or the bare model id.
- `orchestrator_variant: auto` (the default) applies `high` only when models.dev declares it;
  unknown models get no variant. A declared-but-missing variant is dropped with a note, never
  emitted: OpenCode treats an unknown variant as a model-resolution error. `none` disables it.
- `write_plan` copies the effective `scope` into the saved strategy, and `generate`/`check`/
  `agents` prefer `--scope` > strategy > `project`. Preserve that precedence.
- `<models.dev>` is the source of truth for price, `context`, `tool_call` and variants.
  `meta_for` tries `(provider, model_id)` first, then a **unique** model-id match for
  proxies; ambiguous ids return `None` rather than guess a price. `price_of` precedence is
  local (0) → models.dev (exact) → embedded `CATALOG_PRICES` (not exact) → `ASSUMED_PRICE`.
- `cost.estimate` uses one `SESSION_TOKEN_BUDGET` per session shared by the mix; roles with
  no agent are redistributed proportionally, never dropped. Tokens across agents must sum to
  the budget. Tests lock the mixed plan at `$0.3654` / 79.7% savings: removing the
  redistribution reintroduces the old inflated ~92%.
- Tests must stay offline: pass `probe=False` to `build_catalog` (the CLI defaults to probing
  with a 2s timeout). Unreachable endpoints must yield `[]`, never raise.
- Tier values are English in code and `--json` (`frontier`/`cheap`/`local`). Spanish labels
  (`frontera`/`barato`/`local`) are display-only via `TIER_LABELS` / `TIER_TITLES`.
- User-facing text, comments and generated prompts are Spanish; tests assert substrings such as
  `"delega"` and `"sin cerrar"`. Keep them. Identifiers and YAML keys are English
  (`orchestrator_model`), though the loader tolerates `orquestador_model`, `specialist`,
  `especialista`.
- `.orquestador.yaml` is the regeneration contract:
  `preset`, `orchestrator_model`, `orchestrator_variant`, `scope`, `default_agent`,
  `subagents.<fixed|custom>.{model,steps,enabled,...}`, `rules`. `auto`/`null`/`-`
  mean auto; `none` means "no variant". `enabled: false` drops that subagent from the plan
  (its cost share is redistributed); `true`/absent keeps it. `steps` must be a positive
  integer (`StrategyError` otherwise, surfaced by the CLI as exit code 2). `rules` accepts a
  string or a list. `strategy_text` must round-trip custom `description`/`prompt` (JSON-style
  double quotes) and quote `color` (`#abcdef` would otherwise be a YAML comment).

## Gotchas

- "Local" is a hostname heuristic on the provider `baseURL` (`localhost`, `127.0.0.1`,
  `192.168.*`, `10.*`, `172.1*`, `host.docker.internal`), not a config flag.
- Tier guessing order: models.dev price → embedded price catalog → model-name hints →
  parameter count parsed from the id (`\d+b`) → default `cheap`. Embeddings/TTS/ASR models
  are filtered out before reaching `model:`.
- `scan.iter_files` must compare `SKIP_DIRS` against the path relative to the scanned root.
  Matching absolute parts silently returns an empty project when the repo lives under `tmp/`,
  `bin/` or `build/` (there is a regression test for exactly that).
- Dependencies are only `typer`, `rich`, `pyyaml`; probing uses stdlib `urllib`. Do not add
  `requests`.
- Shell resource matching is raw text with wildcards, not a sandbox: `curl *` does not match
  `cd x && curl`. Treat those rules as guardrails in docs and prompts, never as isolation.
- In YAML, an unquoted `*` is an alias and `#abcdef` starts a comment. Quote wildcard
  resources (`resource: "*"`) and custom `color` values; tests parse generated YAML.
- Running `orq init` / `orq generate` inside this repo writes real `.opencode/agents/*` and
  `.orquestador.yaml` here. Use `tmp_path` or a scratch directory instead.
