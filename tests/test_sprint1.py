"""Sprint 1: politica de secretos, variantes verificadas, scope y estrategia robusta."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from orquestador.catalog import build_catalog, meta_for, tool_support
from orquestador.cli import app
from orquestador.planner import (
    Strategy,
    StrategyError,
    build_plan,
    save_strategy,
    strategy_from_mapping,
)
from orquestador.scan import scan_project
from orquestador.templates import render_agent
from orquestador.writer import parse_agent_file, validate, write_plan


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _project(tmp_path):
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / "User.php").write_text("<?php class User {}")
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^12"}}))
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n")
    (tmp_path / ".env").write_text("APP_KEY=secret")
    return tmp_path


def _catalog():
    return build_catalog(
        {
            "provider": {
                "openai": {
                    "options": {"baseURL": "https://api.openai.com/v1"},
                    "models": {"gpt-6-astra": {}, "gpt-6-luna": {}},
                },
                "llamacpp": {
                    "options": {"baseURL": "http://localhost:8080/v1"},
                    "models": {"qwen3.6-35b": {}},
                },
            }
        },
        [],
        probe=False,
    )


def _plan(tmp_path, strategy=None):
    profile = scan_project(_project(tmp_path))
    return build_plan(
        profile,
        _catalog(),
        strategy
        or Strategy(
            orchestrator_model="openai/gpt-6-astra",
            cheap_model="openai/gpt-6-luna",
            local_model="llamacpp/qwen3.6-35b",
        ),
    )


def _front(tmp_path, plan, name):
    agent = next(a for a in plan.agents if a.name == name)
    path = tmp_path / f"{name}.md"
    path.write_text(render_agent(plan, agent), encoding="utf-8")
    return parse_agent_file(path)[0]


def _rules(front):
    return {(r["action"], r.get("resource")): r.get("effect") for r in front.get("permissions", [])}


# --------------------------------------------------------------------------- #
# secretos y external_directory en TODOS los agentes
# --------------------------------------------------------------------------- #


def test_every_agent_denies_external_directory(tmp_path):
    plan = _plan(tmp_path)
    for agent in plan.agents:
        rules = _rules(_front(tmp_path, plan, agent.name))
        assert rules.get(("external_directory", "*")) == "deny", agent.name


def test_every_agent_denies_secret_files(tmp_path):
    plan = _plan(tmp_path)
    for name in ("orquestador", "local", "barato", "especialista"):
        rules = _rules(_front(tmp_path, plan, name))
        assert rules.get(("read", ".env")) == "deny", name
        # el orquestador ya deniega edit "*"; el resto necesita la regla concreta
        assert rules.get(("edit", ".env")) == "deny" or rules.get(("edit", "*")) == "deny", name


def test_local_agent_denies_network_shell(tmp_path):
    plan = _plan(tmp_path)
    front = _front(tmp_path, plan, "local")
    raw = json.dumps(front)
    for pattern in ("curl *", "wget *", "ssh *", "scp *", "git push *"):
        assert pattern in raw
    effective = {(r["action"], r.get("resource")): r.get("effect") for r in front["permissions"]}
    assert effective[("shell", "curl *")] == "deny"


def test_cheap_agent_still_asks_before_editing(tmp_path):
    plan = _plan(tmp_path)
    effective = _rules(_front(tmp_path, plan, "barato"))
    assert effective[("edit", "*")] == "ask"
    assert effective[("shell", "*")] == "ask"
    assert effective[("read", ".env")] == "deny"  # la denegacion concreta gana


# --------------------------------------------------------------------------- #
# variantes: solo si el modelo las declara
# --------------------------------------------------------------------------- #


def test_no_variant_by_default(tmp_path):
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b"))
    assert plan.orchestrator.model == "acme/coyote-77b"


def test_variant_applied_when_declared(models_cache, tmp_path):
    models_cache({
        "acme": {"models": {"coyote-77b": {
            "tool_call": True,
            "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
        }}}
    })
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b",
                                    orchestrator_variant="high"))
    assert plan.orchestrator.model == "acme/coyote-77b#high"
    assert not any("no disponible" in note for note in plan.notes)


def test_auto_high_when_the_model_declares_it(models_cache, tmp_path):
    models_cache({"acme": {"models": {"coyote-77b": {
        "tool_call": True,
        "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
    }}}})
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b"))
    assert plan.orchestrator.model == "acme/coyote-77b#high"
    assert plan.strategy.orchestrator_variant == "auto"


def test_explicit_none_disables_auto_high(models_cache, tmp_path):
    models_cache({"acme": {"models": {"coyote-77b": {
        "reasoning_options": [{"type": "effort", "values": ["high"]}],
    }}}})
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b",
                                    orchestrator_variant="none"))
    assert plan.orchestrator.model == "acme/coyote-77b"


def test_auto_does_not_invent_variants_for_local_models(tmp_path):
    plan = _plan(tmp_path, Strategy(orchestrator_model="llamacpp/qwen3.6-35b"))
    assert plan.orchestrator.model == "llamacpp/qwen3.6-35b"
    assert not any("variante" in note.lower() for note in plan.notes)


def test_unknown_variant_is_dropped_with_note(models_cache, tmp_path):
    models_cache({
        "acme": {"models": {"coyote-77b": {
            "tool_call": True,
            "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
        }}}
    })
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b",
                                    orchestrator_variant="max"))
    assert plan.orchestrator.model == "acme/coyote-77b"
    assert any("max" in note and "no disponible" in note for note in plan.notes)


def test_non_enumerable_variants_are_omitted(models_cache, tmp_path):
    models_cache({
        "acme": {"models": {"coyote-77b": {
            "tool_call": True,
            "reasoning_options": [{"type": "toggle"}],
        }}}
    })
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b",
                                    orchestrator_variant="high"))
    assert plan.orchestrator.model == "acme/coyote-77b"
    assert any("no se pueden enumerar" in note for note in plan.notes)


def test_variant_kept_when_model_is_unknown(models_cache, tmp_path):
    models_cache({"acme": {"models": {}}})
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/misterio",
                                    orchestrator_variant="high"))
    assert plan.orchestrator.model == "acme/misterio#high"
    assert any("No se pudo verificar" in note for note in plan.notes)


def test_inline_variant_is_normalised_in_strategy(models_cache, tmp_path):
    models_cache({"acme": {"models": {"coyote-77b": {
        "reasoning_options": [{"type": "effort", "values": ["high"]}],
    }}}})
    plan = _plan(tmp_path, Strategy(orchestrator_model="acme/coyote-77b#high"))
    assert plan.orchestrator.model == "acme/coyote-77b#high"
    assert plan.strategy.orchestrator_model == "acme/coyote-77b"
    assert plan.strategy.orchestrator_variant == "high"


# --------------------------------------------------------------------------- #
# metadata: tool calling
# --------------------------------------------------------------------------- #


def test_tool_support_uses_models_dev(models_cache):
    models_cache({"acme": {"models": {"coyote-77b": {"tool_call": False}}}})
    model = build_catalog(
        {"provider": {"acme": {"options": {"baseURL": "https://acme.example/v1"},
                               "models": {"coyote-77b": {}}}}},
        [], probe=False,
    ).find("acme/coyote-77b")
    assert tool_support(model) is False
    assert model.supports_tools is False
    assert meta_for("acme/coyote-77b").tool_call is False


# --------------------------------------------------------------------------- #
# estrategia: parsing robusto
# --------------------------------------------------------------------------- #


def test_rules_as_string_is_one_rule():
    strategy = strategy_from_mapping({"rules": "usa el agente local"})
    assert strategy.extra_rules == ["usa el agente local"]


def test_invalid_steps_raise_strategy_error():
    with pytest.raises(StrategyError) as exc:
        strategy_from_mapping({"subagents": {"local": {"steps": "many"}}})
    assert "steps" in str(exc.value)


def test_strategy_roundtrip_escapes_rules(tmp_path):
    path = tmp_path / ".orquestador.yaml"
    strategy = Strategy(orchestrator_model="openai/gpt-6-astra",
                        extra_rules=['di "hola": con comillas', "linea\nnueva"])
    save_strategy(path, strategy, "proyecto")
    loaded = strategy_from_mapping(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert loaded.extra_rules == strategy.extra_rules


def test_variant_defaults_to_auto_in_saved_strategy(tmp_path):
    path = tmp_path / ".orquestador.yaml"
    save_strategy(path, Strategy(orchestrator_model="openai/gpt-6-astra"), "p")
    assert "orchestrator_variant: auto" in path.read_text(encoding="utf-8")


def test_explicit_none_keeps_variant_off(tmp_path):
    path = tmp_path / ".orquestador.yaml"
    save_strategy(path, Strategy(orchestrator_model="openai/gpt-6-astra",
                                 orchestrator_variant="none"), "p")
    assert "orchestrator_variant: none" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# scope: la estrategia recuerda donde se escribio
# --------------------------------------------------------------------------- #


def test_write_plan_records_scope(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    monkeypatch.setattr("orquestador.writer.GLOBAL_CONFIG_DIR", global_dir)
    plan = _plan(tmp_path)
    write_plan(plan, tmp_path, scope="global")
    yaml_text = (tmp_path / ".orquestador.yaml").read_text(encoding="utf-8")
    assert "scope: global" in yaml_text
    assert (global_dir / "agents" / "orquestador.md").exists()


def test_generate_with_missing_strategy_fails(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--dir", str(tmp_path), "--strategy", "nope.yaml", "--no-probe"]
    )
    assert result.exit_code == 2
    assert "No existe la estrategia" in result.stdout


def test_generate_with_broken_yaml_fails_without_traceback(tmp_path):
    broken = tmp_path / "rota.yaml"
    broken.write_text("subagents: [sin: cerrar\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["generate", "--dir", str(tmp_path), "--strategy", str(broken), "--no-probe"]
    )
    assert result.exit_code == 2
    assert "Estrategia invalida" in result.stdout
    assert "Traceback" not in result.stdout


def test_generate_with_invalid_scope_fails(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--dir", str(tmp_path), "--scope", "galaxia", "--no-probe"]
    )
    assert result.exit_code == 2
    assert "scope debe ser" in result.stdout


# --------------------------------------------------------------------------- #
# validacion: modelo inexistente, sin tools y variante invalida
# --------------------------------------------------------------------------- #


def _write_orchestrator(tmp_path, model):
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "orquestador.md").write_text(
        f'---\ndescription: "d"\nmode: primary\nmodel: {model}\npermissions:\n'
        '  - action: edit\n    resource: "*"\n    effect: deny\n'
        '  - action: shell\n    resource: "*"\n    effect: deny\n'
        '  - action: subagent\n    resource: "*"\n    effect: deny\n'
        '  - action: subagent\n    resource: local\n    effect: allow\n---\n\nCuerpo\n',
        encoding="utf-8",
    )
    (agents / "local.md").write_text(
        '---\ndescription: "d"\nmode: subagent\nmodel: llamacpp/qwen3.6-35b\n---\n\nCuerpo\n',
        encoding="utf-8",
    )


def test_validate_errors_on_unknown_model(tmp_path):
    _write_orchestrator(tmp_path, "openai/no-existe")
    problems = validate(tmp_path, catalog=_catalog())
    assert ("error", "orquestador.md: modelo 'openai/no-existe' no existe en la configuracion de OpenCode") in problems


def test_validate_errors_on_orchestrator_without_tools(models_cache, tmp_path):
    models_cache({"openai": {"models": {"gpt-6-astra": {"tool_call": False}}}})
    _write_orchestrator(tmp_path, "openai/gpt-6-astra")
    problems = validate(tmp_path, catalog=_catalog())
    assert any(level == "error" and "no soporta tool calling" in msg for level, msg in problems)


def test_validate_errors_on_unsupported_variant(models_cache, tmp_path):
    models_cache({"openai": {"models": {"gpt-6-astra": {
        "tool_call": True,
        "reasoning_options": [{"type": "effort", "values": ["low"]}],
    }}}})
    _write_orchestrator(tmp_path, "openai/gpt-6-astra#high")
    problems = validate(tmp_path, catalog=_catalog())
    assert any(level == "error" and "variante 'high'" in msg for level, msg in problems)
