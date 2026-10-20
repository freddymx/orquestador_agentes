"""Sprint 6: hardening de seguridad para la publicacion."""

import json

import yaml
from typer.testing import CliRunner

from orquestador import catalog as catalog_mod
from orquestador import writer as writer_mod
from orquestador.catalog import probe_models
from orquestador.cli import app
from orquestador.fsutil import backup_file
from orquestador.planner import (
    AgentPlan,
    CustomAgent,
    Strategy,
    strategy_text,
)
from orquestador.templates import _deny, _frontmatter, provider_snippet

runner = CliRunner()


def test_frontmatter_quotes_model_and_cannot_inject_yaml():
    agent = AgentPlan(
        name="orquestador",
        role="primary",
        tier="frontier",
        model="safe/evil\nmode: subagent",
    )
    text = "\n".join(_frontmatter(agent, "desc")) + "\n"
    data = yaml.safe_load(text)
    assert data["mode"] == "primary"
    assert data["model"] == "safe/evil\nmode: subagent"


def test_deny_quotes_secret_path_with_quotes_and_newlines():
    text = "permissions:\n" + "\n".join(
        _deny("read", '.env"\n    effect: allow')
    ) + "\n"
    data = yaml.safe_load(text)
    assert data["permissions"] == [
        {"action": "read", "resource": '.env"\n    effect: allow', "effect": "deny"}
    ]


def test_provider_snippet_is_valid_json_with_hostile_values():
    snippet = provider_snippet(
        {
            "provider": 'p"\n',
            "baseURL": 'http://x/","injected": true',
            "model_id": 'm"\n',
        }
    )
    data = json.loads("{" + snippet + "}")
    provider = data['p"\n']
    assert provider["name"] == 'p"\n'
    assert provider["options"]["baseURL"] == 'http://x/","injected": true'
    assert 'm"\n' in provider["models"]


def test_strategy_text_quotes_custom_model_injection():
    strategy = Strategy(
        custom_agents=[
            CustomAgent(name="x", model='safe/evil"\n    permissions: full')
        ]
    )
    data = yaml.safe_load(strategy_text(strategy, "demo"))
    assert data["subagents"]["x"]["model"] == 'safe/evil"\n    permissions: full'
    assert "permissions" not in data["subagents"]["x"]


def test_backup_file_refuses_symlink(tmp_path):
    target = tmp_path / "real.jsonc"
    target.write_text('{"apiKey": "x"}\n', encoding="utf-8")
    link = tmp_path / "opencode.jsonc"
    link.symlink_to(target)

    assert backup_file(link) is None
    assert not (tmp_path / "opencode.jsonc.bak").exists()


def test_probe_models_rejects_file_scheme(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("no debe abrir un file://")

    monkeypatch.setattr(catalog_mod.urllib.request, "urlopen", boom)
    assert probe_models("file:///etc/passwd") == []


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = 1\n", encoding="utf-8")
    (tmp_path / "opencode.jsonc").write_text(
        json.dumps(
            {
                "provider": {
                    "openai": {
                        "options": {
                            "baseURL": "https://api.openai.com/v1",
                            "apiKey": "sk-live-secret",
                        },
                        "models": {
                            "gpt-6-astra": {"name": "gpt-6-astra"},
                            "gpt-6-luna": {"name": "gpt-6-luna"},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".orquestador.yaml").write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "subagents:\n"
        "  barato:\n"
        "    model: openai/gpt-6-luna\n",
        encoding="utf-8",
    )
    return tmp_path


def test_diff_does_not_print_config_credentials(tmp_path):
    project = _project(tmp_path)
    result = runner.invoke(app, ["diff", "--dir", str(project)])
    assert result.exit_code == 1, result.output
    assert "sk-live-secret" not in result.output
    assert "diff omitido" in result.output


def test_backup_never_copies_opencode_jsonc(tmp_path):
    from orquestador.planner import build_plan
    from orquestador.scan import scan_project
    from orquestador.writer import write_plan

    project = _project(tmp_path)
    profile = scan_project(project)
    catalog = catalog_mod.build_catalog(
        json.loads((project / "opencode.jsonc").read_text(encoding="utf-8")),
        [project / "opencode.jsonc"],
        probe=False,
    )
    plan = build_plan(profile, catalog, Strategy(orchestrator_model="openai/gpt-6-astra"))
    write_plan(plan, project, force=True, backup=True)
    assert not (project / "opencode.jsonc.bak").exists()


def test_generate_requires_explicit_global_scope(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / ".orquestador.yaml").write_text(
        "orchestrator_model: openai/gpt-6-astra\n"
        "scope: global\n",
        encoding="utf-8",
    )
    global_dir = tmp_path / "global"
    monkeypatch.setattr(writer_mod, "GLOBAL_CONFIG_DIR", global_dir)

    result = runner.invoke(app, ["generate", "--dir", str(project), "--no-probe"])
    assert result.exit_code == 2, result.output
    assert "--scope global" in result.output
    assert not (global_dir / "agents").exists()

    result = runner.invoke(
        app,
        ["generate", "--dir", str(project), "--no-probe", "--scope", "global", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert (global_dir / "agents" / "orquestador.md").exists()


def test_strategy_scalar_normal_values_stay_readable():
    strategy = Strategy(
        orchestrator_model="openai/gpt-6-astra#high",
        orchestrator_variant="high",
        scope="project",
    )
    text = strategy_text(strategy, "demo")
    assert "orchestrator_model: openai/gpt-6-astra#high" in text
    assert "scope: project" in text
