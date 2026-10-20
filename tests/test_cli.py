"""Sprint 4: la CLI de punta a punta (detect, generate, check, agents)."""

import json

import pytest
from typer.testing import CliRunner

from orquestador import catalog as catalog_mod
from orquestador.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_global_config(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "GLOBAL_CONFIG_DIR", tmp_path / "global")


def _project(tmp_path, strategy=True):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "dependencies": {"next": "14"},
            "scripts": {"test": "vitest run", "build": "next build"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = 1\n", encoding="utf-8")
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
    if strategy:
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


def _json(result):
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def test_detect_json_reports_real_stack_and_commands(tmp_path):
    project = _project(tmp_path)

    result = runner.invoke(app, ["detect", "--dir", str(project), "--no-probe", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    detected = payload["project"]
    assert detected["stack"] == "typescript/next"
    assert detected["runtimes"] == ["node"]
    assert detected["frameworks"] == ["next"]
    assert detected["test_command"] == "npx vitest run"
    assert detected["build_command"] == "npm run build"
    assert {model["ref"] for model in payload["models"]} >= {
        "openai/gpt-6-astra",
        "openai/gpt-6-luna",
        "llamacpp/qwen3.6-35b",
    }


def test_generate_dry_run_json_does_not_write(tmp_path):
    project = _project(tmp_path)

    result = runner.invoke(
        app, ["generate", "--dir", str(project), "--no-probe", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [agent["name"] for agent in payload["agents"]] == [
        "orquestador", "local", "barato", "especialista"
    ]
    assert "Eres el orquestador" in payload["agents"][0]["content"]
    assert not (project / ".opencode").exists()
    assert not (project / ".orquestador.yaml.bak").exists()


def test_generate_check_and_agents_roundtrip(tmp_path):
    project = _project(tmp_path)

    generated = runner.invoke(
        app, ["generate", "--dir", str(project), "--no-probe", "--force"]
    )
    assert generated.exit_code == 0, generated.output
    assert (project / ".opencode" / "agents" / "orquestador.md").exists()

    checked = runner.invoke(
        app, ["check", "--dir", str(project), "--no-probe", "--json"]
    )
    assert checked.exit_code == 0, checked.output
    assert _json(checked) == {"ok": True, "problems": []}

    listed = _json(runner.invoke(
        app, ["agents", "--dir", str(project), "--json"]
    ))
    names = {agent["name"] for agent in listed["agents"]}
    assert names == {"orquestador", "local", "barato", "especialista"}
    orchestrator = next(a for a in listed["agents"] if a["name"] == "orquestador")
    assert orchestrator["can_subagent"] == ["barato", "especialista", "local"]

    shown = runner.invoke(
        app, ["agents", "--dir", str(project), "--show", "orquestador"]
    )
    assert shown.exit_code == 0
    assert "Eres el orquestador" in shown.output


def test_check_flags_unknown_model(tmp_path):
    project = _project(tmp_path, strategy=False)
    (project / ".orquestador.yaml").write_text(
        "orchestrator_model: openai/ghost\n"
        "orchestrator_variant: none\n"
        "subagents:\n"
        "  local:\n"
        "    model: llamacpp/qwen3.6-35b\n"
        "  barato:\n"
        "    model: openai/gpt-6-luna\n",
        encoding="utf-8",
    )

    generated = runner.invoke(
        app, ["generate", "--dir", str(project), "--no-probe", "--force"]
    )
    assert generated.exit_code == 1
    assert "no existe" in generated.output

    checked = runner.invoke(
        app, ["check", "--dir", str(project), "--no-probe", "--json"]
    )
    assert checked.exit_code == 1
    payload = json.loads(checked.output)
    assert payload["ok"] is False
    assert any("openai/ghost" in problem["message"] for problem in payload["problems"])


def test_generate_invalid_strategy_exits_2(tmp_path):
    project = _project(tmp_path, strategy=False)
    (project / ".orquestador.yaml").write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "subagents:\n"
        "  local:\n"
        "    steps: muchas\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe"])
    assert result.exit_code == 2
    assert "steps" in result.output
    assert not (project / ".opencode").exists()


def test_agents_without_generated_files_exits_1(tmp_path):
    project = _project(tmp_path, strategy=False)
    result = runner.invoke(app, ["agents", "--dir", str(project), "--json"])
    assert result.exit_code == 1
    assert "orq init" in result.output
