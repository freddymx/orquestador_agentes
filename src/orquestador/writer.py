"""Escritura de ficheros generados y validacion del resultado."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalog import GLOBAL_CONFIG_DIR, meta_for, strip_jsonc, tool_support
from .planner import OrchestrationPlan, Strategy, save_strategy
from .templates import default_agent_snippet, provider_snippet, render_agent

STRATEGY_FILE = ".orquestador.yaml"


@dataclass
class WriteResult:
    files: list[Path]
    skipped: list[Path]
    notes: list[str] = field(default_factory=list)
    strategy_path: Path | None = None


def agents_dir(scope: str, project_dir: Path) -> Path:
    if scope == "global":
        return GLOBAL_CONFIG_DIR / "agents"
    return project_dir / ".opencode" / "agents"


def config_file(scope: str, project_dir: Path) -> Path:
    if scope == "global":
        return GLOBAL_CONFIG_DIR / "opencode.jsonc"
    return project_dir / "opencode.jsonc"


def patch_default_agent(path: Path, agent_name: str) -> bool | None:
    """Anade default_agent a una config existente SIN perder providers/mcp.

    Devuelve True si escribe, None si ya estaba configurado y False si no se puede
    tocar (comentarios JSONC o JSON invalido): reserializarlo destruiria el original.
    """
    import json

    raw = path.read_text(encoding="utf-8")
    if strip_jsonc(raw) != raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("default_agent") == agent_name:
        return None
    data["default_agent"] = agent_name
    ordered = {"$schema": data.pop("$schema")} if "$schema" in data else {}
    ordered.update(data)
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def write_plan(
    plan: OrchestrationPlan,
    project_dir: Path,
    scope: str = "project",
    force: bool = False,
    set_default_agent: bool = True,
    write_strategy: bool = True,
) -> WriteResult:
    target_dir = agents_dir(scope, project_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # La estrategia guardada debe decir donde se escribio de verdad: si no, el
    # siguiente `orq generate` sin --scope escribe en el sitio equivocado.
    plan.strategy.scope = scope

    written: list[Path] = []
    skipped: list[Path] = []
    notes: list[str] = []

    for agent in plan.agents:
        path = target_dir / f"{agent.name}.md"
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(render_agent(plan, agent), encoding="utf-8")
        written.append(path)

    # El opencode.jsonc del usuario NUNCA se sobrescribe a ciegas: contiene sus
    # providers, mcp y claves. --force aplica a los agentes, no a la config.
    cfg = config_file(scope, project_dir)
    if set_default_agent and plan.strategy.default_agent:
        primary = plan.orchestrator
        if not primary:
            notes.append("No hay agente orquestador: no se toca default_agent.")
        elif not cfg.exists():
            cfg.write_text(default_agent_snippet(primary.name), encoding="utf-8")
            written.append(cfg)
        else:
            patched = patch_default_agent(cfg, primary.name)
            if patched:
                written.append(cfg)
                notes.append(f"{cfg.name}: anadido \"default_agent\": \"{primary.name}\" conservando el resto.")
            elif patched is False:
                notes.append(
                    f"{cfg.name} ya existe y tiene comentarios: no se toca para no perderlos.\n"
                    f"  Anade a mano   \"default_agent\": \"{primary.name}\""
                )

    for patch in plan.provider_patches:
        notes.append(
            "Proveedor local detectado sin modelos declarados. Anade este bloque a tu opencode.jsonc:\n"
            f"  {patch['provider']}: {provider_snippet(patch)}"
        )

    strategy_path: Path | None = None
    if write_strategy:
        strategy_path = project_dir / STRATEGY_FILE
        save_strategy(strategy_path, plan.strategy, plan.project)
        written.append(strategy_path)

    return WriteResult(files=written, skipped=skipped, strategy_path=strategy_path, notes=notes)


def write_minimal_config(project_dir: Path, agent_name: str) -> Path:
    path = project_dir / "opencode.jsonc"
    path.write_text(default_agent_snippet(agent_name), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Validacion
# --------------------------------------------------------------------------- #


def parse_agent_file(path: Path) -> tuple[dict, str]:
    """Devuelve (frontmatter como dict, cuerpo). Lanza ValueError si el formato no vale."""
    import yaml

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError("sin frontmatter ---")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter sin cerrar")
    data = yaml.safe_load(parts[1])
    if not isinstance(data, dict):
        raise ValueError("frontmatter no es un mapa")
    return data, parts[2]


def permission_rules(front: dict) -> list[dict]:
    raw = front.get("permissions") or []
    return [rule for rule in raw if isinstance(rule, dict) and "action" in rule]


def effective_permissions(front: dict) -> dict[tuple[str, str], str]:
    """(action, resource) -> effect. Gana la ultima: es la semantica de OpenCode."""
    out: dict[tuple[str, str], str] = {}
    for rule in permission_rules(front):
        out[(str(rule["action"]), str(rule.get("resource", "*")))] = str(rule.get("effect", "allow"))
    return out


def validate(
    project_dir: Path,
    scope: str = "project",
    catalog=None,
    strategy: Strategy | None = None,
) -> list[tuple[str, str]]:
    """Devuelve lista de (nivel, mensaje): nivel 'error' o 'warn'."""
    problems: list[tuple[str, str]] = []
    target_dir = agents_dir(scope, project_dir)
    if not target_dir.is_dir():
        problems.append(("error", f"No existe el directorio de agentes: {target_dir}"))
        return problems

    files = sorted(target_dir.glob("*.md"))
    if not files:
        problems.append(("error", f"No hay agentes en {target_dir}"))
        return problems

    names: set[str] = set()
    fronts: dict[str, dict] = {}
    for path in files:
        name = path.stem
        names.add(name)
        try:
            front, body = parse_agent_file(path)
        except ValueError as exc:
            problems.append(("error", f"{path.name}: {exc}"))
            continue
        fronts[name] = front
        if not front.get("description"):
            problems.append(("error", f"{path.name}: falta 'description' (el padre decide por el)"))
        if not front.get("mode"):
            problems.append(("error", f"{path.name}: falta 'mode'"))
        if not body.strip():
            problems.append(("warn", f"{path.name}: cuerpo vacio (el agente no tiene prompt de sistema)"))
        raw_ref = _raw_ref(front.get("model"))
        ref = _clean_ref(front.get("model"))
        variant = raw_ref.split("#", 1)[1].strip() if raw_ref and "#" in raw_ref else None
        if not ref:
            problems.append(("warn", f"{path.name}: sin 'model' -> heredara el del padre (caro)"))
        elif catalog is not None and catalog.models:
            model = catalog.find(ref)
            if model is None:
                problems.append(
                    ("error", f"{path.name}: modelo '{ref}' no existe en la configuracion de OpenCode")
                )
            elif tool_support(model) is False:
                level = "error" if name == "orquestador" else "warn"
                problems.append(
                    (level, f"{path.name}: '{ref}' no soporta tool calling; no podra usar herramientas")
                )
        if variant:
            meta = meta_for(ref) if ref else None
            if meta is not None and meta.variants_known and variant not in meta.variants:
                disponibles = ", ".join(meta.variants) or "ninguna"
                problems.append(
                    ("error", f"{path.name}: variante '{variant}' no existe para '{ref}' "
                              f"(disponibles: {disponibles})")
                )

    orch = fronts.get("orquestador")
    if orch is not None:
        effective = effective_permissions(orch)
        for action in ("edit", "shell"):
            if effective.get((action, "*")) != "deny":
                problems.append(("error", f"orquestador.md: no deniega '{action}'; deberia coordinar solo"))
        if effective.get(("external_directory", "*")) != "deny":
            problems.append(
                ("warn", "orquestador.md: no deniega external_directory; puede leer fuera del proyecto")
            )
        if effective.get(("subagent", "*")) != "deny":
            problems.append(("error", "orquestador.md: no deniega subagent '*'; podria anidar en exceso"))
        allowed = {
            resource for (action, resource), effect in effective.items()
            if action == "subagent" and effect == "allow"
        }
        if not allowed:
            problems.append(("error", "orquestador.md: no puede llamar a ningun subagente"))
        for target in sorted(allowed):
            if target not in names:
                problems.append(("error", f"orquestador.md: permite '{target}' pero ese agente no existe"))

    for name, front in fronts.items():
        if name == "orquestador":
            continue
        effective = effective_permissions(front)
        if effective.get(("subagent", "*")) == "allow":
            problems.append(("warn", f"{name}.md: permite subagent '*'; conviene cerrar la lista"))
        if effective.get(("external_directory", "*")) != "deny":
            problems.append(
                ("warn", f"{name}.md: no deniega external_directory; puede salir del proyecto")
            )

    cfg = config_file(scope, project_dir)
    if cfg.exists() and "default_agent" not in cfg.read_text(encoding="utf-8"):
        problems.append(("warn", f"{cfg.name}: no define 'default_agent'"))

    if strategy is not None and strategy.local_model and catalog is not None:
        provider_name = strategy.local_model.split("/", 1)[0]
        provider = catalog.providers.get(provider_name)
        if provider is not None and not provider.is_local:
            problems.append(
                ("warn", f"el agente local usa '{provider_name}', que no es un endpoint local")
            )

    return problems


def _raw_ref(value) -> str | None:
    """Modelo tal cual viene del frontmatter, conservando '#variante'."""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip()
    return text or None


def _clean_ref(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').split("#", 1)[0].strip()
    return text or None