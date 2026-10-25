"""Sprint 4: la CLI de punta a puntos (detect, generate, check, agents)."""

import json

from pathlib import Path

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


def test_generate_accepts_endpoint_and_uses_probed_model(tmp_path, monkeypatch):
    """REGRESION: `generate` no aceptaba --endpoint, asi que un modelo que solo
    sirve desde un endpoint sondeado (y no declarado en opencode.jsonc) no podia
    entrar en el plan. Ahora se sondea y se usa."""
    from orquestador import catalog as catalog_mod

    project = tmp_path
    (project / "package.json").write_text(
        json.dumps(
            {"dependencies": {"next": "14"}, "scripts": {"test": "vitest run"}}
        ),
        encoding="utf-8",
    )
    (project / "src").mkdir()
    (project / "src" / "app.ts").write_text("export const app = 1\n", encoding="utf-8")
    # sin opencode.jsonc: el catalogo entero sale del endpoint extra.

    monkeypatch.setattr(catalog_mod, "probe_models", lambda url, timeout=2.0: ["sondeo-modelo"])

    result = runner.invoke(
        app,
        [
            "generate",
            "--dir",
            str(project),
            "--endpoint",
            "http://192.168.1.20:1234/v1",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sondeo-modelo" in result.output

    local = (project / ".opencode" / "agents" / "local.md").read_text()
    assert "sondeo-modelo" in local
    # el modelo sondeado (192.168.x => local) quedo como modelo del agente local
    assert "model: 192-168-1-20-1234/sondeo-modelo" in local


def test_generate_endpoint_alias_uses_readable_provider(tmp_path, monkeypatch):
    """REGRESION (fase b): un endpoint sondeado con alias legible (nombre@url)
    produce refs limpias en vez del slug del host (127-0-0-1-13305)."""
    from orquestador import catalog as catalog_mod

    project = tmp_path
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14"}, "scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )
    (project / "src").mkdir()
    monkeypatch.setattr(catalog_mod, "probe_models", lambda url, timeout=2.0: ["modelo-ejemplo"])

    result = runner.invoke(
        app,
        [
            "generate",
            "--dir",
            str(project),
            "--endpoint",
            "lemonade@http://192.168.1.5:99/v1",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output

    local = (project / ".opencode" / "agents" / "local.md").read_text()
    assert "lemonade/modelo-ejemplo" in local
    assert "192" not in local  # el slug del host no debe aparecer


def _write(path: Path, text: str) -> None:
    """Escribe texto en un fichero de agente ya generado (helper de los tests de check)."""
    path.write_text(text, encoding="utf-8")


def test_check_flags_local_agent_using_cloud_model(tmp_path):
    """FASE C: el agente 'local' exige un endpoint local. Si apunta a un modelo
    cloud, `check` lo marca como ERROR (romperia la privacidad)."""
    project = _project(tmp_path)
    gen = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe", "--force"])
    assert gen.exit_code == 0, gen.output

    local_path = project / ".opencode" / "agents" / "local.md"
    text = local_path.read_text(encoding="utf-8")
    text = text.replace("llamacpp/qwen3.6-35b", "openai/gpt-6-luna")  # cloud en rol local
    _write(local_path, text)

    checked = runner.invoke(app, ["check", "--dir", str(project), "--no-probe", "--json"])
    payload = _json(checked)
    assert payload["ok"] is False
    assert any("privacidad" in p["message"] for p in payload["problems"])


def test_check_warns_nonlocal_role_using_local_model(tmp_path):
    """FASE C: un rol cloud (barato) que usa un modelo local (con alternatives cloud)
    es un AVISO, no un error: saturaria la maquina sin necesidad."""
    project = _project(tmp_path)
    gen = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe", "--force"])
    assert gen.exit_code == 0, gen.output

    barato_path = project / ".opencode" / "agents" / "barato.md"
    text = barato_path.read_text(encoding="utf-8")
    text = text.replace("openai/gpt-6-luna", "llamacpp/qwen3.6-35b")  # local en barato
    _write(barato_path, text)

    checked = runner.invoke(app, ["check", "--dir", str(project), "--no-probe", "--json"])
    payload = _json(checked)
    assert payload["ok"] is True  # es un aviso, no un error
    assert any(
        "barato" in p["message"] and "modelo local" in p["message"] for p in payload["problems"]
    )


def test_check_does_not_flag_local_only_orchestrator_fallback(tmp_path):
    """FASE C: en un entorno con SOLO modelos locales, todos los roles caen a uno
    local (fallback documentado). `check` NO debe marcarlos como rol cloud en rol
    local, aunque existan agentes con el mismo modelo."""
    project = _project(tmp_path)
    # dejar el catalogo y la estrategia 100% locales
    config = json.loads((project / "opencode.jsonc").read_text(encoding="utf-8"))
    config["provider"].pop("openai", None)
    _write(project / "opencode.jsonc", json.dumps(config))
    strategy = (
        "preset: balanced\n"
        "add_local: true\n"
        "local: llamacpp/qwen3.6-35b\n"
        "barato: llamacpp/qwen3.6-35b\n"
        "especialista: llamacpp/qwen3.6-35b\n"
        "orchestrator: llamacpp/qwen3.6-35b\n"
        "orchestrator_variant: none\n"
    )
    _write(project / ".orquestador.yaml", strategy)

    gen = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe", "--force"])
    assert gen.exit_code == 0, gen.output

    checked = runner.invoke(app, ["check", "--dir", str(project), "--no-probe", "--json"])
    payload = _json(checked)
    assert payload["ok"] is True
    messages = " ".join(p["message"] for p in payload["problems"])
    assert "modelo local" not in messages
    assert "privacidad" not in messages


def test_check_warns_orphan_agents_in_leash_mode(tmp_path):
    """FASE C: con preset lean, los agentes que quedaron fuera del plan se marcan como
    huecos con motivo 'preset lean' (aviso), no como error."""
    project = _project(tmp_path)
    gen = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe", "--force"])
    assert gen.exit_code == 0, gen.output

    text = (project / ".orquestador.yaml").read_text(encoding="utf-8")
    text = text.replace("preset: balanced", "preset: lean")  # local sigue siendo el worker
    _write(project / ".orquestador.yaml", text)

    checked = runner.invoke(app, ["check", "--dir", str(project), "--no-probe", "--json"])
    payload = _json(checked)
    assert payload["ok"] is True
    messages = " ".join(p["message"] for p in payload["problems"])
    assert "preset lean" in messages
    assert "barato.md" in messages
    assert "especialista.md" in messages


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


def test_parse_endpoint_supports_alias_and_plain_url():
    from orquestador.catalog import _parse_endpoint

    assert _parse_endpoint("lemonade@http://127.0.0.1:9/v1") == ("lemonade", "http://127.0.0.1:9/v1")
    assert _parse_endpoint("http://127.0.0.1:9/v1") == (None, "http://127.0.0.1:9/v1")
    assert _parse_endpoint("  lemonade@http://127.0.0.1:9/v1 ") == ("lemonade", "http://127.0.0.1:9/v1")
