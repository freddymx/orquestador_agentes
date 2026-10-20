# AGENTS.md

`orq` is a Python 3.11 CLI that inspects a *target* project and writes an OpenCode V2 agent
team into it: `.opencode/agents/*.md`, a `default_agent` patch in `opencode.jsonc`, and
`.orquestador.yaml`. It is not an OpenCode plugin and needs no OpenCode at runtime.

## Commands

- `uv sync` — the only dependency entrypoint. `src/` layout + hatchling means
  `import orquestador` works inside the uv env, not from a bare `python`.
- `uv run pytest` — full suite (~0.3s, 83 tests). One test:
  `uv run pytest tests/test_generate.py::test_force_never_clobbers_existing_config`.
  `tests/conftest.py` points `ORQ_MODELS_CACHE` at a missing file: tests must not depend on
  the machine's models.dev cache. Use the `models_cache` fixture to inject fake metadata.
- There is no ruff/mypy/formatter config and no CI. Do not invent `make lint` or a typecheck
  step; verification is the test suite plus `uv run orq detect --no-probe --json`.
- The global `orq` is installed editable (`uv tool install -e .`), so `src/` edits are live
  without reinstalling. New `[project].dependencies` are the exception: re-run
  `uv tool install -e .` to refresh the tool env.

## Layout (`src/orquestador`)

- `scan.py` — `ProjectProfile`: language counts, frameworks, test/lint/build commands, secret
  files, key dirs. Caps at 4000 files.
- `catalog.py` — merges project + `~/.config/opencode` configs, probes OpenAI-compatible
  `/v1/models`, reads OpenCode's models.dev cache (`~/.cache/opencode/models.json`) for
  `tool_call` and variants, assigns a tier.
- `planner.py` — `Strategy` (from `.orquestador.yaml`) → `auto_strategy` → `build_plan`.
- `templates.py` — agent markdown, YAML frontmatter, prompts (one renderer per agent name).
- `writer.py` — writes agents, patches config, `validate()`.
- `cli.py` — Typer commands and all console output; `cost.py` — cost estimates.

## Invariants tests enforce

- `opencode.jsonc` is never re-serialized when it contains JSONC comments: `patch_default_agent`
  returns `False` and the CLI prints the line to add by hand. `--force` rewrites only agent `.md`
  files. A named regression test covers this — do not "simplify" it away.
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
- Model refs are `provider/model` with optional `#variant`; helpers strip the variant when
  resolving. `Catalog.find` accepts the full ref or the bare model id.
- `orchestrator_variant: auto` (the default) applies `high` only when models.dev declares it;
  unknown models get no variant. A declared-but-missing variant is dropped with a note, never
  emitted: OpenCode treats an unknown variant as a model-resolution error. `none` disables it.
- `write_plan` copies the effective `scope` into the saved strategy, and `generate`/`check`/
  `agents` prefer `--scope` > strategy > `project`. Preserve that precedence.
- Tests must stay offline: pass `probe=False` to `build_catalog` (the CLI defaults to probing
  with a 2s timeout). Unreachable endpoints must yield `[]`, never raise.
- Tier values are English in code and `--json` (`frontier`/`cheap`/`local`). Spanish labels
  (`frontera`/`barato`/`local`) are display-only via `TIER_LABELS` / `TIER_TITLES`.
- User-facing text, comments and generated prompts are Spanish; tests assert substrings such as
  `"delega"` and `"sin cerrar"`. Keep them. Identifiers and YAML keys are English
  (`orchestrator_model`), though the loader tolerates `orquestador_model`, `specialist`,
  `especialista`.
- `.orquestador.yaml` is the regeneration contract:
  `orchestrator_model`, `orchestrator_variant`, `scope`, `default_agent`,
  `subagents.{local,barato,especialista}.{model,steps}`, `rules`. `auto`/`null`/`-` mean
  auto; `none` means "no variant". `steps` must be a positive integer (`StrategyError`
  otherwise, surfaced by the CLI as exit code 2). `rules` accepts a string or a list.

## Gotchas

- "Local" is a hostname heuristic on the provider `baseURL` (`localhost`, `127.0.0.1`,
  `192.168.*`, `10.*`, `172.1*`, `host.docker.internal`), not a config flag.
- Tier guessing order: price catalog → model-name hints → parameter count parsed from the id
  (`\d+b`) → default `cheap`. Embeddings/TTS/ASR models are filtered out before reaching
  `model:`.
- `scan.iter_files` must compare `SKIP_DIRS` against the path relative to the scanned root.
  Matching absolute parts silently returns an empty project when the repo lives under `tmp/`,
  `bin/` or `build/` (there is a regression test for exactly that).
- Dependencies are only `typer`, `rich`, `pyyaml`; probing uses stdlib `urllib`. Do not add
  `requests`.
- Shell resource matching is raw text with wildcards, not a sandbox: `curl *` does not match
  `cd x && curl`. Treat those rules as guardrails in docs and prompts, never as isolation.
- Running `orq init` / `orq generate` inside this repo writes real `.opencode/agents/*` and
  `.orquestador.yaml` here. Use `tmp_path` or a scratch directory instead.
