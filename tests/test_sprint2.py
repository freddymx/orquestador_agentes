"""Sprint 2: precios y tiers desde models.dev, y coste con un presupuesto unico."""

import json

import pytest
from typer.testing import CliRunner

from orquestador import catalog as catalog_mod
from orquestador.catalog import (
    ModelInfo,
    Provider,
    blended_price,
    build_catalog,
    guess_tier,
    meta_for,
    price_of,
)
from orquestador.cli import app
from orquestador.cost import SESSION_TOKEN_BUDGET, estimate
from orquestador.planner import Strategy, build_plan
from orquestador.scan import scan_project


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _provider(name="acme", url="https://acme.example.com/v1"):
    return Provider(name=name, base_url=url)


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


def _cloud_catalog():
    return build_catalog(
        {
            "provider": {
                "openai": {
                    "options": {"baseURL": "https://api.openai.com/v1"},
                    "models": {"gpt-6-astra": {}, "gpt-6-luna": {}},
                }
            }
        },
        [],
        probe=False,
    )


def _plan(tmp_path, catalog=None, **strategy):
    profile = scan_project(tmp_path)
    params = {
        "orchestrator_model": "openai/gpt-6-astra",
        "cheap_model": "openai/gpt-6-luna",
        "local_model": "llamacpp/qwen3.6-35b",
    }
    params.update(strategy)
    return build_plan(profile, catalog or _catalog(), Strategy(**params))


# --------------------------------------------------------------------------- #
# precios y tiers
# --------------------------------------------------------------------------- #


def test_models_dev_price_wins_over_embedded(models_cache):
    models_cache({
        "acme": {"models": {"coyote-77b": {"cost": {"input": 2.0, "output": 8.0}}}}
    })
    model = ModelInfo(provider="acme", model_id="coyote-77b")
    pricing = price_of(model, _provider())
    assert (pricing.input, pricing.output) == (2.0, 8.0)
    assert pricing.exact is True
    assert pricing.source == "models.dev"
    assert blended_price(model, _provider()) == pytest.approx(3.2)


def test_embedded_price_is_not_marked_exact():
    model = ModelInfo(provider="acme", model_id="gpt-6-astra")
    pricing = price_of(model, _provider())
    assert pricing.source == "catalogo embebido"
    assert pricing.exact is False


def test_tier_comes_from_models_dev_price(models_cache):
    models_cache({
        "acme": {"models": {
            "barato-x": {"cost": {"input": 0.2, "output": 0.6}},
            "caro-x": {"cost": {"input": 5.0, "output": 20.0}},
        }}
    })
    assert guess_tier(ModelInfo(provider="acme", model_id="barato-x"), _provider()) == "cheap"
    assert guess_tier(ModelInfo(provider="acme", model_id="caro-x"), _provider()) == "frontier"


def test_context_is_filled_from_models_dev(models_cache):
    models_cache({"acme": {"models": {"coyote-77b": {"limit": {"context": 200_000}}}}})
    catalog = build_catalog(
        {"provider": {"acme": {"options": {"baseURL": "https://acme.example/v1"},
                               "models": {"coyote-77b": {}}}}},
        [], probe=False,
    )
    assert catalog.find("acme/coyote-77b").context == 200_000


def test_model_id_fallback_for_proxies(models_cache):
    models_cache({"openai": {"models": {"gpt-x": {"cost": {"input": 3.0, "output": 9.0}}}}})
    meta = meta_for("proxy/gpt-x")  # el proveedor no esta en models.dev
    assert meta is not None and meta.input_price == 3.0


def test_ambiguous_model_id_is_not_guessed(models_cache):
    models_cache({
        "uno": {"models": {"same": {"cost": {"input": 1.0, "output": 2.0}}}},
        "dos": {"models": {"same": {"cost": {"input": 9.0, "output": 18.0}}}},
    })
    assert meta_for("tres/same") is None


# --------------------------------------------------------------------------- #
# coste: un presupuesto, reparto de roles ausentes
# --------------------------------------------------------------------------- #


def test_budget_covers_all_agents_and_naive(tmp_path):
    est = estimate(_plan(tmp_path), _catalog())
    assert est.token_budget == SESSION_TOKEN_BUDGET
    assert sum(a.tokens for a in est.agents) == SESSION_TOKEN_BUDGET
    assert est.naive_cost == pytest.approx(1.8)  # 100k * 18 $/1M
    assert est.total_cost == pytest.approx(0.3654)
    assert est.savings_ratio == pytest.approx(0.797, abs=1e-3)


def test_absent_role_share_is_redistributed(tmp_path):
    catalog = _cloud_catalog()
    plan = _plan(tmp_path, catalog, local_model=None)
    est = estimate(plan, catalog)
    assert not [a for a in est.agents if a.name == "local"]
    assert sum(a.share for a in est.agents) == pytest.approx(1.0)
    assert any("Sin agente local" in note for note in est.notes)
    # .1 orquestador + .3 especialista al precio caro, .6 barato
    assert est.total_cost == pytest.approx(0.7308)
    assert est.savings_ratio == pytest.approx(0.594, abs=1e-3)


def test_cloud_only_plan_does_not_overstate_savings(tmp_path):
    catalog = _cloud_catalog()
    plan = _plan(tmp_path, catalog, local_model=None)
    est = estimate(plan, catalog)
    assert 0 < est.savings_ratio < 0.8


def test_token_budget_scales_the_comparison(tmp_path):
    full = estimate(_plan(tmp_path), _catalog())
    half = estimate(_plan(tmp_path), _catalog(), token_budget=50_000)
    assert half.token_budget == 50_000
    assert sum(a.tokens for a in half.agents) == 50_000
    assert half.naive_cost == pytest.approx(full.naive_cost / 2)


def test_sessions_multiply_the_budget(tmp_path):
    est = estimate(_plan(tmp_path), _catalog(), sessions=3)
    assert est.token_budget == 3 * SESSION_TOKEN_BUDGET
    assert sum(a.tokens for a in est.agents) == 3 * SESSION_TOKEN_BUDGET


def test_local_orchestrator_with_cloud_workers_reports_spend(tmp_path):
    plan = _plan(tmp_path, orchestrator_model="llamacpp/qwen3.6-35b",
                 local_model=None, add_specialist=False)
    est = estimate(plan, _catalog())
    assert est.naive_cost == 0
    assert est.total_cost > 0
    assert any("gasta" in note for note in est.notes)


def test_estimate_marks_price_sources(tmp_path):
    est = estimate(_plan(tmp_path), _catalog())
    sources = {a.name: a.price_source for a in est.agents}
    assert sources["orquestador"] == "catalogo embebido"
    assert sources["local"] == "local"
    assert any("precios estimados" in note for note in est.notes)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _project_config(tmp_path):
    (tmp_path / "opencode.jsonc").write_text(
        json.dumps({
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
        }),
        encoding="utf-8",
    )
    return tmp_path


def test_compare_json_exposes_budget(tmp_path, monkeypatch):
    _project_config(tmp_path)
    monkeypatch.setattr(catalog_mod, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    result = CliRunner().invoke(
        app, ["compare", "--dir", str(tmp_path), "--no-probe", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["token_budget"] == SESSION_TOKEN_BUDGET
    assert 0 < payload["savings_ratio"] < 0.9
    assert {a["price_source"] for a in payload["agents"]} <= {"models.dev", "catalogo embebido", "local"}


def test_compare_rejects_zero_tokens(tmp_path):
    result = CliRunner().invoke(
        app, ["compare", "--dir", str(tmp_path), "--no-probe", "--tokens", "0"]
    )
    assert result.exit_code == 2
    assert "--tokens" in result.stdout
