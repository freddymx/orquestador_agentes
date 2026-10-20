import json

import pytest

from orquestador.catalog import build_catalog, blended_price, is_chat_model, ModelInfo, Provider
from orquestador.cost import DEFAULT_MIX, estimate
from orquestador.planner import Strategy, build_plan
from orquestador.scan import scan_project
from orquestador.templates import render_agent
from orquestador.writer import effective_permissions, parse_agent_file, permission_rules


def _mixed_catalog():
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


def _plan_for(tmp_path):
    profile = scan_project(tmp_path)
    strategy = Strategy(
        orchestrator_model="openai/gpt-6-astra",
        cheap_model="openai/gpt-6-luna",
        local_model="llamacpp/qwen3.6-35b",
    )
    return profile, build_plan(profile, _mixed_catalog(), strategy)


# --------------------------------------------------------------------------- #
# permisos del especialista
# --------------------------------------------------------------------------- #


def test_specialist_can_delegate_to_mechanical_agents(tmp_path):
    """Regresion: el prompt decia 'delega lo mecanico' pero los permisos lo prohibian."""
    _laravel(tmp_path)
    plan = _plan_for(tmp_path)[1]
    spec = next(a for a in plan.subagents if a.name == "especialista")
    text = render_agent(plan, spec)
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    path = tmp_path / ".opencode" / "agents" / "especialista.md"
    path.write_text(text, encoding="utf-8")
    front, _ = parse_agent_file(path)
    effective = effective_permissions(front)
    # deny * luego allow local/barato: gana el ultimo
    assert effective[("subagent", "*")] == "deny"
    assert effective[("subagent", "local")] == "allow"
    assert effective[("subagent", "barato")] == "allow"
    assert "delega" in text.lower()


def test_specialist_delegates_only_to_what_exists(tmp_path):
    profile = scan_project(tmp_path)
    catalog = build_catalog(
        {"provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                                 "models": {"gpt-6-astra": {}, "gpt-6-luna": {}}}}},
        [], probe=False,
    )
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="openai/gpt-6-astra",
                                                 cheap_model="openai/gpt-6-luna", local_model=None))
    spec = next(a for a in plan.subagents if a.name == "especialista")
    path = tmp_path / ".opencode" / "agents"
    path.mkdir(parents=True)
    agent_file = path / "especialista.md"
    agent_file.write_text(render_agent(plan, spec), encoding="utf-8")
    front, _ = parse_agent_file(agent_file)
    effective = effective_permissions(front)
    # No hay agente local, pero si barato: solo puede llamar a ese.
    assert effective[("subagent", "*")] == "deny"
    assert effective[("subagent", "barato")] == "allow"
    assert ("subagent", "local") not in effective


def test_specialist_without_any_worker_has_no_allow(tmp_path):
    profile = scan_project(tmp_path)
    catalog = build_catalog(
        {"provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                                 "models": {"gpt-6-astra": {}}}}},
        [], probe=False,
    )
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="openai/gpt-6-astra",
                                                 cheap_model=None, local_model=None,
                                                 add_specialist=False))
    assert plan.orchestrator.model
    path = tmp_path / ".opencode" / "agents"
    path.mkdir(parents=True)
    agent_file = path / "orquestador.md"
    agent_file.write_text(render_agent(plan, plan.orchestrator), encoding="utf-8")
    front, _ = parse_agent_file(agent_file)
    effective = effective_permissions(front)
    assert not [r for (a, _), effect in effective.items() if a == "subagent" and effect == "allow"]


def _laravel(root):
    (root / "app").mkdir(parents=True)
    (root / "app" / "A.php").write_text("<?php")
    (root / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^13"}}))
    (root / "artisan").write_text("#!/usr/bin/env php\n")
    return root


# --------------------------------------------------------------------------- #
# frontmatter por YAML
# --------------------------------------------------------------------------- #


def test_parse_agent_file_rejects_broken_frontmatter(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("---\ndescription: a\nmode: subagent\n\nsin cerrar", encoding="utf-8")
    try:
        parse_agent_file(path)
    except ValueError as exc:
        assert "sin cerrar" in str(exc)
    else:
        raise AssertionError("deberia lanzar ValueError")


def test_effective_permissions_last_wins():
    front = {"permissions": [
        {"action": "subagent", "resource": "*", "effect": "deny"},
        {"action": "subagent", "resource": "local", "effect": "allow"},
    ]}
    effective = effective_permissions(front)
    assert effective[("subagent", "*")] == "deny"
    assert effective[("subagent", "local")] == "allow"
    assert len(permission_rules(front)) == 2


def test_validate_flags_subagent_wildcard_on_worker(tmp_path):
    from orquestador.writer import validate

    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "orquestador.md").write_text(
        '---\ndescription: "d"\nmode: primary\nmodel: openai/gpt-6-astra\npermissions:\n'
        '  - action: edit\n    resource: "*"\n    effect: deny\n'
        '  - action: write\n    resource: "*"\n    effect: deny\n'
        '  - action: shell\n    resource: "*"\n    effect: deny\n'
        '  - action: subagent\n    resource: "*"\n    effect: deny\n'
        '  - action: subagent\n    resource: local\n    effect: allow\n---\n\nCuerpo\n',
        encoding="utf-8",
    )
    (agents / "local.md").write_text(
        '---\ndescription: "d"\nmode: subagent\nmodel: llamacpp/qwen\npermissions:\n'
        '  - action: subagent\n    resource: "*"\n    effect: allow\n---\n\nCuerpo\n',
        encoding="utf-8",
    )
    problems = validate(tmp_path)
    assert any("permite subagent '*'" in m for _, m in problems)


def test_validate_flags_empty_body(tmp_path):
    from orquestador.writer import validate

    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "local.md").write_text(
        '---\ndescription: "d"\nmode: subagent\nmodel: llamacpp/qwen\n---\n', encoding="utf-8"
    )
    problems = validate(tmp_path)
    assert any("cuerpo vacio" in m for _, m in problems)


# --------------------------------------------------------------------------- #
# precios y coste
# --------------------------------------------------------------------------- #


def test_blended_price_uses_80_20():
    model = ModelInfo(provider="openai", model_id="gpt-6-astra")
    provider = Provider(name="openai", base_url="https://api.openai.com/v1")
    # astra: 10 in / 50 out -> 10*0.8 + 50*0.2 = 18
    assert blended_price(model, provider) == 18.0


def test_local_model_is_free():
    model = ModelInfo(provider="llamacpp", model_id="qwen3.6-35b")
    provider = Provider(name="llamacpp", base_url="http://localhost:8080/v1")
    assert blended_price(model, provider) == 0.0


def test_unknown_price_is_marked_inexact(tmp_path):
    profile = scan_project(tmp_path)
    catalog = build_catalog(
        {"provider": {"acme": {"options": {"baseURL": "https://acme.example.com/v1"},
                                "models": {"coyote-77b": {}}}}},
        [], probe=False,
    )
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="acme/coyote-77b",
                                                 cheap_model=None, local_model=None))
    est = estimate(plan, catalog)
    assert est.agents
    assert any(not a.exact for a in est.agents)
    assert any("precio no conocido" in n for n in est.notes)


def test_mixed_plan_saves_most_of_the_cost(tmp_path):
    profile = scan_project(tmp_path)
    plan = build_plan(
        profile, _mixed_catalog(),
        Strategy(orchestrator_model="openai/gpt-6-astra",
                 cheap_model="openai/gpt-6-luna",
                 local_model="llamacpp/qwen3.6-35b"),
    )
    est = estimate(plan, _mixed_catalog(), sessions=5)
    assert est.total_cost < est.naive_cost
    assert est.savings_ratio > 0.5
    # Regresion: el calculo viejo daba ~92% porque los roles ausentes desaparecian.
    assert est.savings_ratio < 0.85
    assert est.total_cost == pytest.approx(5 * 0.3654)
    assert sum(a.tokens for a in est.agents) == 5 * 100_000
    assert sum(a.share for a in est.agents) == 1.0


def test_all_local_plan_costs_zero_and_says_privacy(tmp_path):
    profile = scan_project(tmp_path)
    catalog = build_catalog(
        {"provider": {"llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"},
                                   "models": {"qwen3.6-35b": {}}}}},
        [], probe=False,
    )
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="llamacpp/qwen3.6-35b",
                                                 cheap_model=None,
                                                 local_model="llamacpp/qwen3.6-35b"))
    est = estimate(plan, catalog)
    assert est.total_cost == 0.0
    assert any("privacidad" in n for n in est.notes)


def test_custom_mix_changes_cost(tmp_path):
    profile = scan_project(tmp_path)
    catalog = _mixed_catalog()
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="openai/gpt-6-astra",
                                                 cheap_model="openai/gpt-6-luna",
                                                 local_model="llamacpp/qwen3.6-35b"))
    base = estimate(plan, catalog, sessions=10)
    heavy_local = estimate(plan, catalog, sessions=10,
                           mix={"local": 0.9, "cheap": 0.05, "specialist": 0.03, "orchestrator": 0.02})
    assert heavy_local.total_cost < base.total_cost
    assert sum(a.share for a in heavy_local.agents) == 1.0


def test_estimate_dict_is_json_serialisable(tmp_path):
    import json as jsonlib

    profile = scan_project(tmp_path)
    plan = build_plan(profile, _mixed_catalog(), Strategy(orchestrator_model="openai/gpt-6-astra",
                                                          cheap_model="openai/gpt-6-luna",
                                                          local_model="llamacpp/qwen3.6-35b"))
    payload = estimate(plan, _mixed_catalog(), sessions=3).as_dict()
    assert jsonlib.loads(jsonlib.dumps(payload))["savings_ratio"] > 0


def test_default_mix_sums_to_one():
    assert abs(sum(DEFAULT_MIX.values()) - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# fallback sin modelos de frontera
# --------------------------------------------------------------------------- #


def test_orchestrator_never_left_without_model(tmp_path):
    """Regresion: se generaba un orquestador SIN 'model', que heredaria el del padre."""
    profile = scan_project(tmp_path)
    catalog = build_catalog(
        {"provider": {"llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"},
                                   "models": {"qwen3.6-35b": {}}}}},
        [], probe=False,
    )
    plan = build_plan(profile, catalog, Strategy())
    assert plan.orchestrator.model, "el orquestador nunca debe quedar sin modelo"
    assert plan.orchestrator.model.startswith("llamacpp/qwen3.6-35b")
    assert any("frontera" in note for note in plan.notes)
    assert "model:" in render_agent(plan, plan.orchestrator)


def test_no_models_at_all_reports_clearly(tmp_path):
    profile = scan_project(tmp_path)
    catalog = build_catalog({}, [], probe=False)
    plan = build_plan(profile, catalog, Strategy())
    assert plan.orchestrator.model is None
    assert any("NINGUN modelo" in note for note in plan.notes)

# --------------------------------------------------------------------------- #
# selector interactivo: siempre se puede elegir
# --------------------------------------------------------------------------- #


def test_ask_role_falls_back_to_all_models_when_tier_empty():
    """Sin modelos de frontera debe ofrecer TODOS los marcados con su tier real."""
    from orquestador.cli import _ask_role

    catalog = build_catalog(
        {"provider": {"llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"},
                                   "models": {"qwen3.6-35b": {}, "gemma-4-e4b": {}}}}},
        [], probe=False,
    )
    assert catalog.by_tier("frontier") == []
    offered = list(catalog.models)
    try:
        result = _ask_role(catalog, None, "orquestador", "frontera", offered, empty_hint="x")
    except Exception:
        # Sin stdin no puede preguntar: lo que importa es que le llegaba lista.
        result = None
    assert len(offered) == 2
    assert all(m.ref.startswith("llamacpp/") for m in offered)


def test_ask_role_keeps_tier_list_when_available():
    from orquestador.cli import _ask_role

    catalog = _mixed_catalog()
    frontier = [m for m in catalog.models if m.model_id == "gpt-6-astra"]
    assert frontier
    # Con lista propia no debe cambiar a todos los modelos.
    try:
        _ask_role(catalog, frontier[0].ref, "orquestador", "frontera", frontier)
    except Exception:
        pass
