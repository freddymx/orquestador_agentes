"""Sprint 3: JSONC sin perder comentarios, escritura atomica, diff, backups y agentes off."""

import json

import pytest
from typer.testing import CliRunner

from orquestador.catalog import build_catalog, strip_jsonc
from orquestador.cli import app
from orquestador.fsutil import atomic_write_text
from orquestador.planner import (
    Strategy,
    build_plan,
    load_strategy,
    save_strategy,
    strategy_text,
)
from orquestador.scan import scan_project
from orquestador.writer import patch_default_agent, planned_files, write_plan

runner = CliRunner()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _config():
    return {
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
    }


def _catalog():
    return build_catalog(_config(), [], probe=False)


def _plan(tmp_path, **strategy):
    params = {
        "orchestrator_model": "openai/gpt-6-astra",
        "cheap_model": "openai/gpt-6-luna",
        "local_model": "llamacpp/qwen3.6-35b",
    }
    params.update(strategy)
    return build_plan(scan_project(tmp_path), _catalog(), Strategy(**params))


def _strategy_file(tmp_path, extra=""):
    path = tmp_path / ".orquestador.yaml"
    path.write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "orchestrator_variant: none\n"
        "subagents:\n"
        "  local:\n"
        "    model: llamacpp/qwen3.6-35b\n"
        "  barato:\n"
        "    model: openai/gpt-6-luna\n"
        + extra,
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# JSONC: insertar y actualizar sin reserializar
# --------------------------------------------------------------------------- #


def test_insert_preserves_comments_providers_and_mcp(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    original = (
        "{\n"
        "  // mi config\n"
        '  "provider": {"llamacpp": {"models": {"qwen": {}}}},\n'
        "  /* bloque\n"
        "     multilinea */\n"
        '  "mcp": {"MCP_DOCKER": {"type": "local"}}\n'
        "}\n"
    )
    cfg.write_text(original, encoding="utf-8")

    assert patch_default_agent(cfg, "orquestador") is True
    text = cfg.read_text(encoding="utf-8")
    assert "// mi config" in text
    assert "/* bloque" in text and "multilinea */" in text
    data = json.loads(strip_jsonc(text))
    assert data["default_agent"] == "orquestador"
    assert data["provider"]["llamacpp"]["models"] == {"qwen": {}}
    assert data["mcp"]["MCP_DOCKER"]["type"] == "local"


def test_insert_single_line_config(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text('{"provider": {"x": {}}}', encoding="utf-8")

    assert patch_default_agent(cfg, "orquestador") is True
    data = json.loads(strip_jsonc(cfg.read_text(encoding="utf-8")))
    assert data["default_agent"] == "orquestador"
    assert data["provider"] == {"x": {}}


def test_insert_empty_object(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text("{}\n", encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is True
    assert json.loads(strip_jsonc(cfg.read_text(encoding="utf-8"))) == {
        "default_agent": "orquestador"
    }

    cfg.write_text("{ /* vacio */ }\n", encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is True
    text = cfg.read_text(encoding="utf-8")
    assert "vacio" in text
    assert json.loads(strip_jsonc(text))["default_agent"] == "orquestador"


def test_update_existing_key_preserves_comments(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(
        '{\n  // keep\n  "default_agent": "otro",\n  "provider": {}\n}\n',
        encoding="utf-8",
    )

    assert patch_default_agent(cfg, "orquestador") is True
    text = cfg.read_text(encoding="utf-8")
    assert "// keep" in text
    data = json.loads(strip_jsonc(text))
    assert data["default_agent"] == "orquestador"
    assert data["provider"] == {}
    assert text.count('"default_agent"') == 1


def test_existing_value_is_a_noop(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    original = '{\n  "default_agent": "orquestador"\n}\n'
    cfg.write_text(original, encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is None
    assert cfg.read_text(encoding="utf-8") == original


def test_non_string_existing_value_is_replaced(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text('{"default_agent": null, "provider": {}}', encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is True
    data = json.loads(strip_jsonc(cfg.read_text(encoding="utf-8")))
    assert data["default_agent"] == "orquestador"


def test_invalid_json_is_never_touched(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    original = '{"provider": {,}\n'
    cfg.write_text(original, encoding="utf-8")
    assert patch_default_agent(cfg, "orquestador") is False
    assert cfg.read_text(encoding="utf-8") == original


def test_planned_files_keeps_comments_in_config_patch(tmp_path):
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(
        '{\n  // no perder\n  "provider": {"openai": {"models": {"gpt-6-astra": {}}}}\n}\n',
        encoding="utf-8",
    )
    files, notes = planned_files(_plan(tmp_path), tmp_path)
    configs = [item for item in files if item.kind == "config-patch"]
    assert len(configs) == 1
    assert "// no perder" in configs[0].text
    assert json.loads(strip_jsonc(configs[0].text))["default_agent"] == "orquestador"
    assert notes == []


# --------------------------------------------------------------------------- #
# escritura atomica y backups
# --------------------------------------------------------------------------- #


def test_atomic_write_leaves_no_temporaries(tmp_path):
    target = tmp_path / "sub" / "agente.md"
    atomic_write_text(target, "hola\n")
    assert target.read_text(encoding="utf-8") == "hola\n"
    assert list(target.parent.glob("*.tmp")) == []
    assert list(target.parent.glob(".*.tmp")) == []


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    target = tmp_path / "agente.md"
    target.write_text("original\n", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr("orquestador.fsutil.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "nuevo\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_plan_backup_keeps_previous_agent(tmp_path):
    plan = _plan(tmp_path)
    write_plan(plan, tmp_path, force=True)
    agent = tmp_path / ".opencode" / "agents" / "local.md"
    agent.write_text("version vieja\n", encoding="utf-8")

    result = write_plan(plan, tmp_path, force=True, backup=True)
    assert agent in result.files
    assert agent.with_name("local.md.bak").read_text(encoding="utf-8") == "version vieja\n"
    assert "version vieja" not in agent.read_text(encoding="utf-8")


def test_write_plan_without_backup_does_not_create_bak(tmp_path):
    write_plan(_plan(tmp_path), tmp_path, force=True)
    assert list(tmp_path.rglob("*.bak")) == []


def test_save_strategy_backup(tmp_path):
    path = tmp_path / ".orquestador.yaml"
    strategy = Strategy(orchestrator_model="openai/gpt-6-astra")
    path.write_text("vieja\n", encoding="utf-8")
    save_strategy(path, strategy, "proyecto", backup=True)
    assert path.with_name(".orquestador.yaml.bak").read_text(encoding="utf-8") == "vieja\n"


# --------------------------------------------------------------------------- #
# agentes desactivados
# --------------------------------------------------------------------------- #


def test_disabled_local_is_not_planned(tmp_path):
    plan = _plan(tmp_path, add_local=False)
    names = [agent.name for agent in plan.agents]
    assert "local" not in names
    assert "barato" in names
    assert any("local desactivado" in note for note in plan.notes)


def test_disabled_barato_is_not_planned(tmp_path):
    plan = _plan(tmp_path, add_cheap=False)
    names = [agent.name for agent in plan.agents]
    assert "barato" not in names
    assert any("barato desactivado" in note for note in plan.notes)


def test_enabled_false_roundtrip(tmp_path):
    path = tmp_path / ".orquestador.yaml"
    path.write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "subagents:\n"
        "  local:\n"
        "    enabled: false\n"
        '  barato:\n'
        '    enabled: "false"\n',
        encoding="utf-8",
    )
    strategy = load_strategy(path)
    assert strategy.add_local is False
    assert strategy.add_cheap is False
    assert strategy.add_specialist is True

    text = strategy_text(strategy, "proyecto")
    assert text.count("enabled: false") == 2

    save_strategy(path, strategy, "proyecto")
    assert load_strategy(path).add_local is False


def test_generate_skips_disabled_agents(tmp_path):
    _strategy_file(
        tmp_path,
        "  especialista:\n    model: openai/gpt-6-astra\n",
    )
    strategy_path = tmp_path / ".orquestador.yaml"
    text = strategy_path.read_text(encoding="utf-8")
    text = text.replace(
        "  local:\n    model: llamacpp/qwen3.6-35b\n",
        "  local:\n    model: llamacpp/qwen3.6-35b\n    enabled: false\n",
    )
    strategy_path.write_text(text, encoding="utf-8")
    (tmp_path / "opencode.jsonc").write_text(json.dumps(_config()), encoding="utf-8")

    result = runner.invoke(
        app, ["generate", "--dir", str(tmp_path), "--no-probe", "--force"]
    )
    assert result.exit_code == 0, result.output
    agents = tmp_path / ".opencode" / "agents"
    assert (agents / "orquestador.md").exists()
    assert (agents / "barato.md").exists()
    assert not (agents / "local.md").exists()


def test_disabling_an_existing_agent_warns_about_stale_file(tmp_path):
    (tmp_path / "opencode.jsonc").write_text(json.dumps(_config()), encoding="utf-8")
    _strategy_file(tmp_path, "  especialista:\n    model: openai/gpt-6-astra\n")
    write_plan(_plan(tmp_path), tmp_path, force=True)
    assert (tmp_path / ".opencode" / "agents" / "local.md").exists()

    strategy_path = tmp_path / ".orquestador.yaml"
    strategy_path.write_text(
        strategy_path.read_text(encoding="utf-8").replace(
            "  local:\n    model: llamacpp/qwen3.6-35b\n",
            "  local:\n    model: llamacpp/qwen3.6-35b\n    enabled: false\n",
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["generate", "--dir", str(tmp_path), "--no-probe"])
    assert result.exit_code == 0, result.output
    assert "local.md" in result.output
    assert "enabled: false" in result.output
    assert (tmp_path / ".opencode" / "agents" / "local.md").exists()


# --------------------------------------------------------------------------- #
# orq diff
# --------------------------------------------------------------------------- #


def test_diff_reports_added_files_and_exit_1(tmp_path):
    _strategy_file(tmp_path)

    result = runner.invoke(app, ["diff", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "orquestador.md" in result.output
    assert "/dev/null" in result.output

    payload = runner.invoke(app, ["diff", "--dir", str(tmp_path), "--json"])
    data = json.loads(payload.output)
    assert data["ok"] is False
    assert data["differences"] >= 3
    statuses = {item["path"].split("/")[-1]: item["status"] for item in data["files"]}
    assert statuses["orquestador.md"] == "added"
    assert statuses["barato.md"] == "added"
    assert statuses[".orquestador.yaml"] == "modified"


def test_diff_is_clean_after_generate(tmp_path):
    plan = _plan(tmp_path)
    write_plan(plan, tmp_path, force=True)

    result = runner.invoke(app, ["diff", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Sin diferencias" in result.output

    payload = json.loads(runner.invoke(app, ["diff", "--dir", str(tmp_path), "--json"]).output)
    assert payload["ok"] is True
    assert payload["differences"] == 0


def test_diff_detects_a_modified_agent(tmp_path):
    plan = _plan(tmp_path)
    write_plan(plan, tmp_path, force=True)
    agent = tmp_path / ".opencode" / "agents" / "barato.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nmanual\n", encoding="utf-8")

    result = runner.invoke(app, ["diff", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "---" in result.output and "+++" in result.output
    assert "-manual" in result.output

    payload = json.loads(runner.invoke(app, ["diff", "--dir", str(tmp_path), "--json"]).output)
    statuses = {item["path"].split("/")[-1]: item["status"] for item in payload["files"]}
    assert statuses["barato.md"] == "modified"
    assert statuses["orquestador.md"] == "unchanged"


def test_diff_does_not_write_anything(tmp_path):
    _strategy_file(tmp_path)
    before = {path for path in tmp_path.rglob("*")}

    runner.invoke(app, ["diff", "--dir", str(tmp_path)])

    after = {path for path in tmp_path.rglob("*")}
    assert before == after
    assert not (tmp_path / ".opencode").exists()
