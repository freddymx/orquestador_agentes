import json

import pytest

from orquestador.catalog import (
    build_catalog,
    guess_tier,
    merge_configs,
    probe_models,
    strip_jsonc,
)
from orquestador.catalog import ModelInfo, Provider
from orquestador.scan import scan_project
from orquestador.planner import Strategy, build_plan, strategy_from_mapping


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


def test_strip_jsonc_removes_comments_and_trailing_commas():
    raw = """
    {
      // comentario
      "a": 1, /* otro */
      "b": [1, 2,],
      "c": "no // soy comentario",
    }
    """
    assert json.loads(strip_jsonc(raw)) == {"a": 1, "b": [1, 2], "c": "no // soy comentario"}


def test_merge_config_prefers_project_over_global(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "project"
    global_dir.mkdir()
    project_dir.mkdir()
    (global_dir / "opencode.json").write_text(
        json.dumps({"default_agent": "global", "provider": {"x": {"models": {"m1": {}}}}})
    )
    (project_dir / "opencode.jsonc").write_text(
        "// config del proyecto\n" + json.dumps({"default_agent": "proyecto"})
    )
    merged, sources = merge_configs(project_dir, global_dir)
    assert merged["default_agent"] == "proyecto"
    assert "x" in merged["provider"]  # el global se conserva
    assert len(sources) == 2


def test_extract_providers_supports_both_singular_and_plural(tmp_path):
    config = {
        "provider": {"lemonade": {"options": {"baseURL": "http://192.168.31.93:13305/v1"},
                                  "models": {"M1": {"name": "M1"}}}},
        "providers": {"openai": {"options": {"baseURL": "https://api.openai.com/v1", "apiKey": "sk"}}},
    }
    catalog = build_catalog(config, [], probe=False)
    assert set(catalog.providers) == {"lemonade", "openai"}
    assert catalog.providers["lemonade"].is_local is True
    assert catalog.providers["openai"].is_local is False
    assert catalog.find("lemonade/M1") is not None


def test_probe_models_handles_unreachable_host(monkeypatch):
    monkeypatch.setattr("orquestador.catalog.urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert probe_models("http://localhost:65000/v1", timeout=0.1) == []


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("gpt-6-astra", "frontier"),
        ("gpt-6-luna", "cheap"),
        ("deepseek-v4-flash", "cheap"),
        ("minimax-M2.5", "cheap"),
    ],
)
def test_guess_tier_cloud_models(model_id, expected):
    model = ModelInfo(provider="openai", model_id=model_id)
    assert guess_tier(model, Provider(name="openai", base_url="https://api.openai.com/v1")) == expected


def test_guess_tier_local_endpoint_wins():
    model = ModelInfo(provider="llamacpp", model_id="qwen3.6-35b")
    provider = Provider(name="llamacpp", base_url="http://localhost:8080/v1")
    assert guess_tier(model, provider) == "local"


def test_guess_tier_by_size_heuristic():
    provider = Provider(name="p", base_url="https://cloud.example.com/v1")
    assert guess_tier(ModelInfo(provider="p", model_id="big-70b-instruct"), provider) == "frontier"
    assert guess_tier(ModelInfo(provider="p", model_id="tiny-3b"), provider) == "cheap"


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #


def _make_laravel(root):
    (root / "app" / "Models").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "resources" / "views").mkdir(parents=True)
    (root / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^12"}}))
    (root / "artisan").write_text("#!/usr/bin/env php\n")
    (root / "phpunit.xml").write_text("<phpunit/>")
    (root / ".env").write_text("APP_KEY=secret")
    (root / "app" / "Models" / "User.php").write_text("<?php class User {}")
    (root / "tests" / "Feature" if False else root / "tests").mkdir(exist_ok=True)


def test_scan_laravel(tmp_path):
    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    assert "laravel" in profile.frameworks
    assert profile.primary_language == "php"
    assert profile.test_command == "./vendor/bin/phpunit"
    assert ".env" in profile.secret_paths
    assert "app" in profile.key_dirs
    assert any(s.kind == "secrets" for s in profile.signals)


def test_scan_node_vitest(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "lint": "eslint"}}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export const a = 1")
    profile = scan_project(tmp_path)
    assert "node" in profile.frameworks
    assert profile.test_command == "npx vitest run"
    assert profile.languages["typescript"] == 1


def test_scan_ignores_vendor(tmp_path):
    (tmp_path / "vendor" / "pkg").mkdir(parents=True)
    (tmp_path / "vendor" / "pkg" / "x.php").write_text("<?php")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "y.py").write_text("x=1")
    profile = scan_project(tmp_path)
    assert profile.languages.get("php") is None
    assert profile.languages["python"] == 1


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #


def test_strategy_from_mapping_reads_subagents():
    strategy = strategy_from_mapping({
        "orchestrator_model": "openai/gpt-6-astra",
        "subagents": {
            "local": {"model": "llamacpp/qwen", "steps": 50},
            "barato": {"model": "openai/gpt-6-luna"},
            "especialista": {"enabled": False},
        },
        "rules": ["usa local para grep"],
    })
    assert strategy.orchestrator_model == "openai/gpt-6-astra"
    assert strategy.local_steps == 50
    assert strategy.add_specialist is False
    assert strategy.extra_rules == ["usa local para grep"]


def test_build_plan_generates_four_agents(tmp_path):
    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    catalog = build_catalog({
        "providers": {
            "openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                       "models": {"gpt-6-astra": {}, "gpt-6-luna": {}}},
            "llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"},
                         "models": {"qwen3.6-35b": {}}},
        }
    }, [], probe=False)
    plan = build_plan(profile, catalog)
    assert [a.name for a in plan.agents] == ["orquestador", "local", "barato", "especialista"]
    assert plan.orchestrator.model == "openai/gpt-6-astra"  # sin cache: no se inventa variante
    local = next(a for a in plan.subagents if a.name == "local")
    assert local.model == "llamacpp/qwen3.6-35b"
    assert set(plan.delegation_targets) == {"local", "barato", "especialista"}


def test_build_plan_without_local_model(tmp_path):
    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    catalog = build_catalog({
        "provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                                "models": {"gpt-6-astra": {}, "gpt-6-luna": {}}}}
    }, [], probe=False)
    plan = build_plan(profile, catalog, Strategy(local_model=None))
    assert "local" not in plan.delegation_targets
    assert any("local" in note for note in plan.notes)


def test_provider_patch_for_local_without_declared_models(tmp_path):
    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    catalog = build_catalog({
        "provider": {"llamacpp": {"options": {"baseURL": "http://localhost:8080/v1"}, "models": {}}}
    }, [], probe=False)
    strategy = Strategy(orchestrator_model="openai/gpt-6-astra",
                        cheap_model="openai/gpt-6-luna",
                        local_model="llamacpp/qwen3.6-35b")
    plan = build_plan(profile, catalog, strategy)
    assert plan.provider_patches
    assert plan.provider_patches[0]["baseURL"] == "http://localhost:8080/v1"

# --------------------------------------------------------------------------- #
# regresiones
# --------------------------------------------------------------------------- #


def test_scan_not_broken_by_skip_dir_in_absolute_path(tmp_path, monkeypatch):
    """Regresion: comparar SKIP_DIRS contra la ruta ABSOLUTA vaciaba el proyecto
    cuando la ruta contenia 'tmp', 'bin', 'build'... (bug real en /tmp/demo-laravel)."""
    root = tmp_path / "tmp" / "bin" / "build" / "proyecto"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1")
    profile = scan_project(root)
    assert profile.languages["python"] == 1


def test_scan_still_skips_nested_vendor(tmp_path):
    root = tmp_path / "app"
    (root / "vendor" / "p").mkdir(parents=True)
    (root / "vendor" / "p" / "x.php").write_text("<?php")
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "User.php").write_text("<?php")
    profile = scan_project(root)
    assert profile.languages["php"] == 1


def test_non_chat_models_excluded(tmp_path):
    config = {"providers": {"lemonade": {"options": {"baseURL": "http://192.168.1.5:13305/v1"},
                                          "models": {"Whisper-Large-v3-Turbo": {},
                                                     "nomic-embed-text-v1-GGUF": {},
                                                     "Qwen3.8-27B-GGUF": {}}}}}
    catalog = build_catalog(config, [], probe=False)
    ids = {m.model_id for m in catalog.models}
    assert ids == {"Qwen3.8-27B-GGUF"}
    assert len(catalog.excluded) == 2


def test_auto_value_is_not_used_as_model_name(tmp_path):
    from orquestador.planner import build_plan

    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    catalog = build_catalog({"provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                                                         "models": {"gpt-6-astra": {}, "gpt-6-luna": {}}}}},
                            [], probe=False)
    strategy = Strategy(orchestrator_model="openai/gpt-6-astra",
                        cheap_model="openai/gpt-6-luna",
                        local_model="auto")
    plan = build_plan(profile, catalog, strategy)
    assert all(a.model != "auto" for a in plan.agents)
    assert "local" not in plan.delegation_targets


def test_orchestrator_variant_applied_and_not_inherited(tmp_path):
    from orquestador.planner import build_plan

    _make_laravel(tmp_path)
    profile = scan_project(tmp_path)
    catalog = build_catalog({"provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1"},
                                                         "models": {"gpt-6-astra": {}, "gpt-6-luna": {}}}}},
                            [], probe=False)
    strategy = Strategy(orchestrator_model="openai/gpt-6-astra", orchestrator_variant="high",
                        cheap_model="openai/gpt-6-luna", local_model=None)
    plan = build_plan(profile, catalog, strategy)
    assert plan.orchestrator.model == "openai/gpt-6-astra#high"
    spec = next(a for a in plan.subagents if a.name == "especialista")
    assert spec.model == "openai/gpt-6-astra"  # sin #high
