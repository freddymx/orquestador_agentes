import json

from orquestador.catalog import build_catalog
from orquestador.planner import Strategy, build_plan
from orquestador.scan import scan_project
from orquestador.templates import render_agent
from orquestador.writer import validate, write_plan


def _laravel_project(tmp_path):
    (tmp_path / "app" / "Models").mkdir(parents=True)
    (tmp_path / "tests" / "Feature").mkdir(parents=True)
    (tmp_path / "resources" / "views").mkdir(parents=True)
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^12"}}))
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n")
    (tmp_path / "pest.php").write_text("<?php // pest")
    (tmp_path / ".env").write_text("APP_KEY=secret")
    (tmp_path / "app" / "Models" / "User.php").write_text("<?php class User {}")
    (tmp_path / "opencode.jsonc").write_text(
        json.dumps({
            "provider": {
                "llamacpp": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "http://localhost:8080/v1"},
                    "models": {"qwen3.6-35b": {"name": "Qwen local"}},
                }
            }
        })
    )
    return tmp_path


def _plan_for(project_dir):
    profile = scan_project(project_dir)
    config = json.loads((project_dir / "opencode.jsonc").read_text())
    config["providers"] = {
        "openai": {
            "options": {"baseURL": "https://api.openai.com/v1", "apiKey": "x"},
            "models": {"gpt-6-astra": {"name": "gpt-6-astra"}, "gpt-6-luna": {"name": "gpt-6-luna"}},
        }
    }
    catalog = build_catalog(config, [project_dir / "opencode.jsonc"], probe=False)
    return build_plan(profile, catalog, Strategy())


def test_orchestrator_denies_editing_and_allows_subagents(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    text = render_agent(plan, plan.orchestrator)
    for action in ("edit", "subagent", "shell", "external_directory"):
        assert f"  - action: {action}\n    resource: \"*\"\n    effect: deny" in text
    for target in plan.delegation_targets:
        assert f"resource: {target}\n    effect: allow" in text
    assert 'action: read\n    resource: ".env"' in text  # no lee secretos
    assert "default_agent" not in text  # va en opencode.jsonc
    assert "`./vendor/bin/pest`" in text  # el comando de tests aparece en el prompt


def test_local_agent_denies_network_and_secrets(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    local = next(a for a in plan.subagents if a.name == "local")
    text = render_agent(plan, local)
    assert "action: webfetch" in text and "effect: deny" in text
    assert "action: websearch" in text
    assert 'resource: ".env"' in text
    assert "mode: subagent" in text


def test_cheap_agent_uses_ask_for_edit_and_shell(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    cheap = next(a for a in plan.subagents if a.name == "barato")
    text = render_agent(plan, cheap)
    assert "  - action: edit\n    resource: \"*\"\n    effect: ask" in text
    assert "  - action: shell\n    resource: \"*\"\n    effect: ask" in text


def test_specialist_can_edit_but_not_internet(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    spec = next(a for a in plan.subagents if a.name == "especialista")
    text = render_agent(plan, spec)
    front = text.split("---")[1]
    assert 'action: edit\n    resource: "*"' not in front  # puede editar el proyecto
    assert 'action: edit\n    resource: ".env"\n    effect: deny' in front  # pero no secretos
    assert 'action: read\n    resource: ".env"\n    effect: deny' in front
    assert "action: subagent\n    resource: \"*\"\n    effect: deny" in text
    assert "Laravel" in text


def test_write_and_validate_roundtrip(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    result = write_plan(plan, tmp_path, scope="project", force=False)
    names = {p.name for p in result.files}
    assert {"orquestador.md", "local.md", "barato.md", "especialista.md", ".orquestador.yaml"} <= names

    catalog = build_catalog({}, [], probe=False)
    problems = validate(tmp_path, scope="project", catalog=catalog, strategy=plan.strategy)
    errors = [m for level, m in problems if level == "error"]
    assert errors == [], errors


def test_write_does_not_overwrite_without_force(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "orquestador.md").write_text("contenido previo")
    result = write_plan(plan, tmp_path, force=False)
    assert (agents / "orquestador.md").read_text() == "contenido previo"
    assert agents / "orquestador.md" in result.skipped

    write_plan(plan, tmp_path, force=True)
    assert (agents / "orquestador.md").read_text() != "contenido previo"


def test_strategy_file_roundtrip(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    write_plan(plan, tmp_path)
    strategy_file = tmp_path / ".orquestador.yaml"
    text = strategy_file.read_text()
    assert "orchestrator_model: openai/gpt-6-astra" in text
    assert "steps: 40" in text
    from orquestador.planner import load_strategy

    reloaded = load_strategy(strategy_file)
    assert reloaded.orchestrator_model == plan.strategy.orchestrator_model
    assert reloaded.local_model == plan.strategy.local_model
    assert reloaded.local_steps == plan.strategy.local_steps


def test_validate_flags_orchestrator_without_permissions(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    write_plan(plan, tmp_path)
    bad = tmp_path / ".opencode" / "agents" / "orquestador.md"
    bad.write_text("---\ndescription: \"x\"\nmode: primary\n---\n\nCuerpo\n")
    problems = validate(tmp_path, scope="project")
    assert any("no deniega 'edit'" in m for _, m in problems)

def test_rules_do_not_render_double_dash(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    text = render_agent(plan, plan.orchestrator)
    assert "- - " not in text


def test_generated_agents_are_nonempty(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    for agent in plan.agents:
        text = render_agent(plan, agent)
        assert len(text.split("---", 2)[2].strip()) > 200, agent.name


def test_validate_reads_model_value_per_line(tmp_path):
    """Regresion: el validador se comia el resto del frontmatter tras 'model:'."""
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    write_plan(plan, tmp_path)
    problems = validate(tmp_path, scope="project", catalog=None)
    assert not [m for _, m in problems if "no aparece en el catalogo" in m]
    assert not [m for level, m in problems if level == "error"]


def test_generated_frontmatter_has_single_blank_line(tmp_path):
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    text = render_agent(plan, plan.orchestrator)
    assert text.split("---", 2)[2].startswith("\nEres el orquestador")
    assert text.endswith("\n")
    for agent in plan.agents[1:]:
        other = render_agent(plan, agent)
        assert other.split("---", 2)[2].startswith("\n")


# --------------------------------------------------------------------------- #
# la config del usuario es intocable
# --------------------------------------------------------------------------- #


def test_force_never_clobbers_existing_config(tmp_path):
    """REGRESION: --force sobrescribia opencode.jsonc y perdia los providers."""
    _laravel_project(tmp_path)
    plan = _plan_for(tmp_path)
    cfg = tmp_path / "opencode.jsonc"
    original = cfg.read_text()
    write_plan(plan, tmp_path, force=True)
    current = cfg.read_text()
    assert '"default_agent": "orquestador"' in current
    # el contenido original debe seguir dentro (providers intactos)
    assert "llamacpp" in current
    assert "192.168.31.93" in current or "localhost:8080" in current
    assert "Qwen local" in current  # no se pierde ningun modelo declarado
    assert original.strip() != ""


def test_patch_default_agent_preserves_providers(tmp_path):
    from orquestador.writer import patch_default_agent

    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {"llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"},
                                   "models": {"qwen": {}}}},
        "mcp": {"MCP_DOCKER": {"type": "local"}},
    }, indent=2), encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is True
    data = json.loads(cfg.read_text())
    assert data["default_agent"] == "orquestador"
    assert data["provider"]["llamacpp"]["models"] == {"qwen": {}}
    assert data["mcp"]["MCP_DOCKER"]["type"] == "local"
    assert data["$schema"].startswith("https://")


def test_patch_default_agent_refuses_when_comments_present(tmp_path):
    from orquestador.writer import patch_default_agent

    cfg = tmp_path / "opencode.jsonc"
    original = '{\n  // mi config\n  "provider": {}\n}\n'
    cfg.write_text(original, encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is False
    assert cfg.read_text() == original  # intacto


def test_patch_default_agent_is_idempotent(tmp_path):
    from orquestador.writer import patch_default_agent

    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(json.dumps({"$schema": "x", "default_agent": "orquestador"}), encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is None
