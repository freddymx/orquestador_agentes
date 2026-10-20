"""Sprint 5: presets de jerarquia y subagentes personalizados."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from orquestador import catalog as catalog_mod
from orquestador.catalog import build_catalog
from orquestador.cli import app
from orquestador.cost import estimate
from orquestador.planner import (
    CustomAgent,
    Strategy,
    StrategyError,
    build_plan,
    normalize_preset,
    strategy_from_mapping,
    strategy_text,
)
from orquestador.scan import scan_project
from orquestador.templates import render_agent
from orquestador.writer import planned_files, validate, write_plan

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_global_config(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "GLOBAL_CONFIG_DIR", tmp_path / "global")


def _config():
    return {
        "providers": {
            "openai": {
                "options": {"baseURL": "https://api.openai.com/v1"},
                "models": {
                    "gpt-6-astra": {"name": "gpt-6-astra"},
                    "gpt-6-luna": {"name": "gpt-6-luna"},
                },
            },
            "llamacpp": {
                "options": {"baseURL": "http://localhost:8080/v1"},
                "models": {"qwen3.6-35b": {"name": "Qwen local"}},
            },
        }
    }


def _catalog():
    return build_catalog(_config(), [], probe=False)


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("APP_KEY=secret\n", encoding="utf-8")
    return scan_project(tmp_path)


def _strategy(**overrides):
    data = {
        "orchestrator_model": "openai/gpt-6-astra",
        "orchestrator_variant": "none",
        "local_model": "llamacpp/qwen3.6-35b",
        "cheap_model": "openai/gpt-6-luna",
        "specialist_model": "openai/gpt-6-astra",
    }
    data.update(overrides)
    return Strategy(**data)


def _plan(tmp_path, **overrides):
    return build_plan(_project(tmp_path), _catalog(), _strategy(**overrides))


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


def test_preset_defaults_to_balanced_and_accepts_auto():
    assert Strategy().preset == "balanced"
    assert normalize_preset(None) == "balanced"
    assert normalize_preset("auto") == "balanced"
    assert normalize_preset("LEAN") == "lean"
    for invalid in ("turbo", 0, False):
        with pytest.raises(StrategyError, match="preset"):
            normalize_preset(invalid)


def test_lean_keeps_only_the_local_worker(tmp_path):
    plan = _plan(tmp_path, preset="lean")

    assert [agent.name for agent in plan.agents] == ["orquestador", "local"]
    assert any("Preset lean" in note and "local" in note for note in plan.notes)


def test_lean_prefers_cheap_when_local_is_disabled(tmp_path):
    plan = _plan(tmp_path, preset="lean", add_local=False)

    assert [agent.name for agent in plan.agents] == ["orquestador", "barato"]


def test_lean_falls_back_to_specialist(tmp_path):
    plan = _plan(tmp_path, preset="lean", add_local=False, add_cheap=False)

    assert [agent.name for agent in plan.agents] == ["orquestador", "especialista"]


def test_lean_skips_custom_agents(tmp_path):
    plan = _plan(
        tmp_path,
        preset="lean",
        custom_agents=[CustomAgent(name="frontend", model="openai/gpt-6-astra")],
    )

    assert "frontend" not in [agent.name for agent in plan.agents]
    assert any("subagente(s) personalizado(s)" in note for note in plan.notes)


def test_strict_prompts_do_not_ask_agents_to_run_shell(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    plan = _plan(tmp_path, preset="strict")
    by_name = {agent.name: agent for agent in plan.agents}

    for name in ("local", "barato", "especialista"):
        body = render_agent(plan, by_name[name])
        assert "No tienes shell" in body
        assert "Antes de dar por terminado, ejecuta" not in body


def test_lean_warns_about_stale_generated_agents(tmp_path):
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "barato.md").write_text("viejo\n", encoding="utf-8")
    (agents / "frontend.md").write_text("viejo\n", encoding="utf-8")

    plan = _plan(
        tmp_path,
        preset="lean",
        custom_agents=[CustomAgent(name="frontend", model="openai/gpt-6-astra")],
    )
    _, notes = planned_files(plan, tmp_path)

    assert any("barato.md" in note and "preset lean" in note for note in notes)
    assert any("frontend.md" in note and "preset lean" in note for note in notes)


def test_strict_hardens_subagent_permissions(tmp_path):
    plan = _plan(tmp_path, preset="strict")
    by_name = {agent.name: agent for agent in plan.agents}

    orchestrator = render_agent(plan, by_name["orquestador"]).split("---", 2)[1]
    assert 'action: webfetch\n    resource: "*"\n    effect: deny' in orchestrator
    assert 'action: websearch\n    resource: "*"\n    effect: deny' in orchestrator

    local = render_agent(plan, by_name["local"]).split("---", 2)[1]
    assert 'action: shell\n    resource: "*"\n    effect: deny' in local

    cheap = render_agent(plan, by_name["barato"]).split("---", 2)[1]
    assert 'action: shell\n    resource: "*"\n    effect: deny' in cheap
    assert 'action: edit\n    resource: "*"\n    effect: ask' in cheap

    specialist = render_agent(plan, by_name["especialista"]).split("---", 2)[1]
    assert 'action: shell\n    resource: "*"\n    effect: deny' in specialist
    assert 'action: edit\n    resource: "*"\n    effect: ask' in specialist


# --------------------------------------------------------------------------- #
# Subagentes personalizados
# --------------------------------------------------------------------------- #


def test_custom_agent_is_planned_and_routed(tmp_path):
    custom = CustomAgent(
        name="frontend",
        model="openai/gpt-6-astra",
        steps=25,
        tier="frontier",
        description="Implementa y revisa UI",
        prompt="Trabaja solo en src/components.",
    )
    plan = _plan(tmp_path, custom_agents=[custom])
    by_name = {agent.name: agent for agent in plan.agents}

    assert by_name["frontend"].tier == "frontier"
    assert by_name["frontend"].steps == 25
    text = render_agent(plan, by_name["frontend"])
    assert 'description: "Implementa y revisa UI"' in text
    assert "Trabaja solo en src/components." in text
    assert "Eres el subagente `frontend`" in text

    orchestrator = render_agent(plan, by_name["orquestador"])
    assert "`frontend` (openai/gpt-6-astra): Implementa y revisa UI" in orchestrator
    assert "resource: frontend\n    effect: allow" in orchestrator


def test_custom_agent_auto_model_uses_requested_tier(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[CustomAgent(name="ayudante", tier="cheap", description="Ayuda")],
    )
    agent = next(a for a in plan.agents if a.name == "ayudante")

    assert agent.model == "openai/gpt-6-luna"
    assert any("modelo automatico" in note for note in plan.notes)


def test_custom_explicit_local_tier_never_falls_back_to_cloud(tmp_path):
    config = _config()
    del config["providers"]["llamacpp"]
    catalog = build_catalog(config, [], probe=False)
    plan = build_plan(
        _project(tmp_path),
        catalog,
        _strategy(custom_agents=[CustomAgent(name="privado", tier="local")]),
    )

    assert "privado" not in [agent.name for agent in plan.agents]
    assert any("tier local" in note for note in plan.notes)


def test_custom_agent_disabled_is_not_planned(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[CustomAgent(name="apagado", model="openai/gpt-6-astra", enabled=False)],
    )

    assert "apagado" not in [agent.name for agent in plan.agents]
    assert any("'apagado' desactivado" in note for note in plan.notes)


def test_custom_agent_read_permissions_are_enforced(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[
            CustomAgent(
                name="auditor",
                model="openai/gpt-6-luna",
                tier="cheap",
                permissions="read",
                description="Audita seguridad",
            )
        ],
    )
    auditor = next(a for a in plan.agents if a.name == "auditor")
    front = render_agent(plan, auditor).split("---", 2)[1]

    assert 'action: edit\n    resource: "*"\n    effect: deny' in front
    assert 'action: shell\n    resource: "*"\n    effect: deny' in front
    assert "Solo lectura" in render_agent(plan, auditor)


def test_custom_agent_full_permissions_allow_edit_and_shell(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[
            CustomAgent(
                name="operador",
                model="openai/gpt-6-astra",
                permissions="full",
                description="Opera el proyecto",
            )
        ],
    )
    operador = next(a for a in plan.agents if a.name == "operador")
    front = render_agent(plan, operador).split("---", 2)[1]

    assert 'action: edit\n    resource: "*"\n    effect: allow' in front
    assert 'action: shell\n    resource: "*"\n    effect: allow' in front


def test_strict_forces_custom_agents_to_ask_and_deny_shell(tmp_path):
    plan = _plan(
        tmp_path,
        preset="strict",
        custom_agents=[
            CustomAgent(
                name="operador",
                model="openai/gpt-6-astra",
                permissions="full",
                description="Opera el proyecto",
            )
        ],
    )
    operador = next(a for a in plan.agents if a.name == "operador")
    front = render_agent(plan, operador).split("---", 2)[1]

    assert 'action: edit\n    resource: "*"\n    effect: ask' in front
    assert 'action: shell\n    resource: "*"\n    effect: deny' in front


def test_custom_agent_roundtrip_preserves_prompt_and_permissions(tmp_path):
    custom = CustomAgent(
        name="ui",
        model=None,
        steps=22,
        enabled=False,
        tier="local",
        permissions="read",
        description='Disena "UI"\ncon cuidado',
        prompt="Linea 1\nLinea 2",
        color="#abcdef",
    )
    text = strategy_text(_strategy(preset="strict", custom_agents=[custom]), "demo")
    data = yaml.safe_load(text)

    assert data["preset"] == "strict"
    assert data["subagents"]["ui"]["prompt"] == "Linea 1\nLinea 2"
    loaded = strategy_from_mapping(data)
    reloaded = loaded.custom_agents[0]
    assert reloaded.name == "ui"
    assert reloaded.model is None
    assert reloaded.steps == 22
    assert reloaded.enabled is False
    assert reloaded.tier == "local"
    assert reloaded.permissions == "read"
    assert reloaded.description == 'Disena "UI"\ncon cuidado'
    assert reloaded.prompt == "Linea 1\nLinea 2"
    assert reloaded.color == "#abcdef"


@pytest.mark.parametrize(
    "name,spec",
    [
        ("bad name", {}),
        ("1frontend", {}),
        ("orquestador", {}),
        ("ok", {"tier": "medium"}),
        ("ok", {"permissions": "write"}),
        ("ok", {"permissions": 0}),
        ("ok", {"color": "red"}),
        ("ok", {"color": 0}),
        ("ok", {"steps": 0}),
        ("ok", {"steps": True}),
        ("ok", {"description": 123}),
        ("ok", {"prompt": ["no"]}),
        ("ok", "no-es-un-mapa"),
    ],
)
def test_invalid_custom_agents_raise_strategy_error(name, spec):
    with pytest.raises(StrategyError):
        strategy_from_mapping({"subagents": {name: spec}})


def test_custom_agent_names_are_unique_after_normalizing():
    with pytest.raises(StrategyError, match="duplicado"):
        strategy_from_mapping(
            {"subagents": {"frontend": {}, "Frontend": {}}}
        )


def test_custom_agents_get_tokens_from_the_shared_budget(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[
            CustomAgent(name="frontend", model="openai/gpt-6-astra", tier="frontier"),
            CustomAgent(name="auditor", model="openai/gpt-6-luna", tier="cheap"),
        ],
    )
    result = estimate(plan, _catalog())

    assert sum(agent.tokens for agent in result.agents) == result.token_budget
    assert {"frontend", "auditor"} <= {agent.name for agent in result.agents}


def test_writer_generates_and_validates_custom_agent(tmp_path):
    plan = _plan(
        tmp_path,
        custom_agents=[
            CustomAgent(
                name="frontend",
                model="openai/gpt-6-astra",
                tier="frontier",
                description="Implementa UI",
                prompt="Trabaja en src.",
            )
        ],
    )
    result = write_plan(plan, tmp_path, force=True)

    custom_file = tmp_path / ".opencode" / "agents" / "frontend.md"
    assert custom_file in result.files
    assert custom_file.exists()
    problems = validate(tmp_path, catalog=_catalog(), strategy=plan.strategy)
    assert not [message for level, message in problems if level == "error"]
    assert not [message for level, message in problems if "no deniega" in message]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = 1\n", encoding="utf-8")
    (tmp_path / "opencode.jsonc").write_text(json.dumps(_config()), encoding="utf-8")
    (tmp_path / ".orquestador.yaml").write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "orchestrator_variant: none\n"
        "subagents:\n"
        "  local:\n"
        "    model: llamacpp/qwen3.6-35b\n"
        "  barato:\n"
        "    model: openai/gpt-6-luna\n"
        "  especialista:\n"
        "    model: openai/gpt-6-astra\n",
        encoding="utf-8",
    )
    return tmp_path


def test_cli_generate_preset_lean(tmp_path):
    project = _cli_project(tmp_path)

    result = runner.invoke(
        app,
        ["generate", "--dir", str(project), "--no-probe", "--preset", "lean", "--force"],
    )
    assert result.exit_code == 0, result.output
    agents = {path.stem for path in (project / ".opencode" / "agents").glob("*.md")}
    assert agents == {"orquestador", "local"}
    assert "preset: lean" in (project / ".orquestador.yaml").read_text(encoding="utf-8")


def test_cli_check_after_lean_reports_stale_agents_not_delegation(tmp_path):
    project = _cli_project(tmp_path)
    assert runner.invoke(
        app, ["generate", "--dir", str(project), "--no-probe", "--force"]
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["generate", "--dir", str(project), "--no-probe", "--preset", "lean", "--force"],
    ).exit_code == 0

    result = runner.invoke(
        app, ["check", "--dir", str(project), "--no-probe", "--json"]
    )
    assert result.exit_code == 0, result.output
    messages = [problem["message"] for problem in json.loads(result.output)["problems"]]
    assert not [message for message in messages if "no puede delegar" in message]
    assert [message for message in messages if "barato.md no esta en el plan actual" in message]


def test_cli_init_with_lean_preset(tmp_path):
    project = _cli_project(tmp_path)

    result = runner.invoke(
        app,
        ["init", "--dir", str(project), "--no-probe", "--preset", "lean"],
        input="\n" * 10,
    )

    assert result.exit_code == 0, result.output
    agents = {path.stem for path in (project / ".opencode" / "agents").glob("*.md")}
    assert agents == {"orquestador", "local"}
    assert "preset: lean" in (project / ".orquestador.yaml").read_text(encoding="utf-8")


def test_cli_generate_rejects_unknown_preset(tmp_path):
    result = runner.invoke(
        app, ["generate", "--dir", str(tmp_path), "--no-probe", "--preset", "turbo"]
    )

    assert result.exit_code == 2
    assert "preset" in result.output


def test_cli_generate_dry_run_json_includes_custom_agents(tmp_path):
    project = _cli_project(tmp_path)
    strategy = project / ".orquestador.yaml"
    strategy.write_text(
        strategy.read_text(encoding="utf-8")
        + "  frontend:\n"
        "    model: openai/gpt-6-astra\n"
        "    steps: 18\n"
        "    description: Implementa UI\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["generate", "--dir", str(project), "--no-probe", "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["preset"] == "balanced"
    frontend = next(agent for agent in payload["agents"] if agent["name"] == "frontend")
    assert frontend["steps"] == 18
    assert "Implementa UI" in frontend["content"]
