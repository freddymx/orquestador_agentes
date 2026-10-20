"""Escritura de ficheros generados y validacion del resultado."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from .catalog import GLOBAL_CONFIG_DIR, meta_for, strip_jsonc, tool_support
from .fsutil import atomic_write_text, backup_file
from .planner import OrchestrationPlan, Strategy, strategy_text
from .scan import ProjectProfile, scan_project
from .templates import default_agent_snippet, provider_snippet, render_agent

STRATEGY_FILE = ".orquestador.yaml"
GENERATED_AGENTS = ("orquestador", "local", "barato", "especialista")


@dataclass
class WriteResult:
    files: list[Path]
    skipped: list[Path]
    notes: list[str] = field(default_factory=list)
    strategy_path: Path | None = None


@dataclass
class PlannedFile:
    path: Path
    text: str
    kind: str  # agent | config-new | config-patch | strategy


def agents_dir(scope: str, project_dir: Path) -> Path:
    if scope == "global":
        return GLOBAL_CONFIG_DIR / "agents"
    return project_dir / ".opencode" / "agents"


def config_file(scope: str, project_dir: Path) -> Path:
    if scope == "global":
        return GLOBAL_CONFIG_DIR / "opencode.jsonc"
    return project_dir / "opencode.jsonc"


# --------------------------------------------------------------------------- #
# Edicion de JSONC preservando comentarios
# --------------------------------------------------------------------------- #


def _read_string(text: str, start: int) -> int | None:
    """`start` apunta a la comilla de apertura; devuelve el indice tras la de cierre."""
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        i += 1
    return None


def _skip_ws_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            break
    return i


def _root_brace(text: str) -> int:
    i = _skip_ws_comments(text, 0)
    return i if i < len(text) and text[i] == "{" else -1


def _raw_value_end(text: str, start: int) -> int | None:
    i, depth = start, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            end = _read_string(text, i)
            if end is None:
                return None
            i = end
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0:
            return i
        i += 1
    return i


def _find_top_level_value(text: str, key: str) -> tuple[int, int] | None:
    """Posicion (inicio, fin) del valor de `key` en el objeto raiz."""
    depth, i = 0, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            end = _read_string(text, i)
            if end is None:
                return None
            if depth == 1:
                try:
                    name = json.loads(text[i:end])
                except json.JSONDecodeError:
                    name = None
                if name == key:
                    colon = _skip_ws_comments(text, end)
                    if colon < len(text) and text[colon] == ":":
                        start = _skip_ws_comments(text, colon + 1)
                        if start < len(text) and text[start] == '"':
                            value_end = _read_string(text, start)
                            return (start, value_end) if value_end else None
                        value_end = _raw_value_end(text, start)
                        return (start, value_end) if value_end is not None else None
            i = end
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return None


def _insert_after_root(text: str, agent_name: str) -> str:
    """Inserta la clave tras la llave raiz: no toca ni una linea del resto."""
    open_brace = _root_brace(text)
    if open_brace < 0:
        raise ValueError("la raiz no es un objeto")

    line_start = text.rfind("\n", 0, open_brace) + 1
    prefix = text[line_start:open_brace]
    root_indent = prefix if not prefix.strip() else ""
    after = text[open_brace + 1:]
    child_indent = root_indent + "  "
    newline = after.find("\n")
    if newline >= 0:
        candidate = re.match(r"[ \t]*", after[newline + 1:])
        if candidate and candidate.group(0):
            child_indent = candidate.group(0)

    entry = f'"default_agent": {json.dumps(agent_name, ensure_ascii=False)}'
    next_code = _skip_ws_comments(text, open_brace + 1)
    if next_code < len(text) and text[next_code] == "}":
        # Objeto vacio: se inserta antes del cierre conservando comentarios/huecos.
        gap = text[open_brace + 1:next_code]
        newline_prefix = "" if gap.endswith(("\n", "\r\n")) else "\n"
        return (
            text[:next_code]
            + newline_prefix
            + f"{child_indent}{entry}\n{root_indent}"
            + text[next_code:]
        )

    if after.startswith(("\n", "\r\n")):
        insertion = f"\n{child_indent}{entry},"
    else:
        insertion = f"\n{child_indent}{entry},\n{child_indent}"
    return text[: open_brace + 1] + insertion + after


def patched_config_text(raw: str, agent_name: str) -> str:
    """Devuelve la config con `default_agent` puesto, conservando comentarios.

    Lanza ValueError si el fichero no es JSONC valido o no se puede editar con
    seguridad. Nunca reserializa el documento entero.
    """
    try:
        data = json.loads(strip_jsonc(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON invalido: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("la raiz no es un objeto")
    if data.get("default_agent") == agent_name:
        return raw

    if "default_agent" in data:
        span = _find_top_level_value(raw, "default_agent")
        if span is None:
            raise ValueError("no se pudo localizar 'default_agent' en el texto")
        start, end = span
        new = raw[:start] + json.dumps(agent_name, ensure_ascii=False) + raw[end:]
    else:
        new = _insert_after_root(raw, agent_name)

    try:
        json.loads(strip_jsonc(new))
    except json.JSONDecodeError as exc:
        raise ValueError(f"el resultado no seria JSON valido: {exc}") from None
    return new


def patch_default_agent(path: Path, agent_name: str, backup: bool = False) -> bool | None:
    """Anade o actualiza `default_agent` sin perder providers, mcp ni comentarios.

    Devuelve True si escribe, None si ya estaba configurado y False si no se puede
    tocar (JSON invalido, raiz que no es objeto...).
    """
    raw = path.read_text(encoding="utf-8")
    try:
        new = patched_config_text(raw, agent_name)
    except ValueError:
        return False
    if new == raw:
        return None
    if backup:
        backup_file(path)
    atomic_write_text(path, new)
    return True


# --------------------------------------------------------------------------- #
# Plan de escritura (compartido por generate y diff)
# --------------------------------------------------------------------------- #


def planned_files(
    plan: OrchestrationPlan,
    project_dir: Path,
    scope: str = "project",
    set_default_agent: bool = True,
    write_strategy: bool = True,
) -> tuple[list[PlannedFile], list[str]]:
    """Ficheros que escribiria `write_plan`, sin tocar el disco.

    Devuelve tambien las notas que no son ficheros (por ejemplo, una config que no
    se puede editar y hay que tocar a mano).
    """
    plan.strategy.scope = scope
    target_dir = agents_dir(scope, project_dir)
    files = [
        PlannedFile(target_dir / f"{agent.name}.md", render_agent(plan, agent), "agent")
        for agent in plan.agents
    ]
    notes: list[str] = []

    if target_dir.is_dir():
        planned_names = {agent.name for agent in plan.agents}
        custom_names = {custom.name for custom in plan.strategy.custom_agents}
        generated_names = set(GENERATED_AGENTS) | custom_names
        for path in sorted(target_dir.glob("*.md")):
            if path.stem in generated_names and path.stem not in planned_names:
                if plan.strategy.preset == "lean":
                    reason = "preset lean"
                elif path.stem in custom_names:
                    custom = next(c for c in plan.strategy.custom_agents if c.name == path.stem)
                    reason = "enabled: false" if not custom.enabled else "fuera del plan"
                else:
                    enabled = {
                        "local": plan.strategy.add_local,
                        "barato": plan.strategy.add_cheap,
                        "especialista": plan.strategy.add_specialist,
                    }.get(path.stem, True)
                    reason = "enabled: false" if not enabled else "fuera del plan"
                notes.append(
                    f"{path.name} sigue en disco pero ya no esta en el plan "
                    f"({reason}). Borralo si quieres desactivarlo del todo."
                )

    if set_default_agent and plan.strategy.default_agent:
        primary = plan.orchestrator
        if primary is None:
            notes.append("No hay agente orquestador: no se toca default_agent.")
        else:
            cfg = config_file(scope, project_dir)
            if not cfg.exists():
                files.append(PlannedFile(cfg, default_agent_snippet(primary.name), "config-new"))
            else:
                try:
                    raw = cfg.read_text(encoding="utf-8")
                    new = patched_config_text(raw, primary.name)
                except (OSError, ValueError):
                    new = None
                if new is None:
                    notes.append(
                        f"{cfg.name} no es JSONC valido: no se toca para no perderlo.\n"
                        f"  Anade a mano   \"default_agent\": \"{primary.name}\""
                    )
                elif new != raw:
                    files.append(PlannedFile(cfg, new, "config-patch"))

    if write_strategy:
        files.append(PlannedFile(
            project_dir / STRATEGY_FILE, strategy_text(plan.strategy, plan.project), "strategy"
        ))
    return files, notes


def write_plan(
    plan: OrchestrationPlan,
    project_dir: Path,
    scope: str = "project",
    force: bool = False,
    set_default_agent: bool = True,
    write_strategy: bool = True,
    backup: bool = False,
) -> WriteResult:
    target_dir = agents_dir(scope, project_dir)
    files, notes = planned_files(plan, project_dir, scope, set_default_agent, write_strategy)

    written: list[Path] = []
    skipped: list[Path] = []
    strategy_path: Path | None = None
    if any(item.kind == "agent" for item in files):
        target_dir.mkdir(parents=True, exist_ok=True)

    for item in files:
        if item.kind == "agent" and item.path.exists() and not force:
            skipped.append(item.path)
            continue
        if backup:
            backup_file(item.path)
        atomic_write_text(item.path, item.text)
        written.append(item.path)
        if item.kind == "strategy":
            strategy_path = item.path
        elif item.kind == "config-patch":
            agent_name = plan.orchestrator.name if plan.orchestrator is not None else "orquestador"
            notes.append(
                f"{item.path.name}: anadido \"default_agent\": \"{agent_name}\" "
                "conservando el resto."
            )

    for patch in plan.provider_patches:
        notes.append(
            "Proveedor local detectado sin modelos declarados. Anade este bloque a tu opencode.jsonc:\n"
            f"  {patch['provider']}: {provider_snippet(patch)}"
        )

    return WriteResult(files=written, skipped=skipped, strategy_path=strategy_path, notes=notes)


def write_minimal_config(project_dir: Path, agent_name: str) -> Path:
    path = project_dir / "opencode.jsonc"
    atomic_write_text(path, default_agent_snippet(agent_name))
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


def permission_effect(front: dict, action: str, resource: str) -> str | None:
    """Evalua una regla concreta respetando comodines y el orden (gana la ultima)."""
    effect: str | None = None
    for rule in permission_rules(front):
        if str(rule["action"]) != action:
            continue
        pattern = str(rule.get("resource", "*"))
        if pattern == "*" or pattern == resource or fnmatchcase(resource, pattern):
            effect = str(rule.get("effect", "allow"))
    return effect


def validate(
    project_dir: Path,
    scope: str = "project",
    catalog=None,
    strategy: Strategy | None = None,
    profile: ProjectProfile | None = None,
    planned_names: set[str] | None = None,
    report_stale: bool = True,
) -> list[tuple[str, str]]:
    """Devuelve lista de (nivel, mensaje): nivel 'error' o 'warn'.

    `planned_names` permite distinguir agentes que ya no pertenecen al plan actual
    (por ejemplo con `preset: lean`) de agentes escritos a mano.
    """
    problems: list[tuple[str, str]] = []
    target_dir = agents_dir(scope, project_dir)
    if not target_dir.is_dir():
        problems.append(("error", f"No existe el directorio de agentes: {target_dir}"))
        return problems
    secret_paths = (profile or scan_project(project_dir)).secret_paths

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

    stale_generated: set[str] = set()
    if planned_names is not None:
        known = set(GENERATED_AGENTS)
        if strategy is not None:
            known |= {custom.name for custom in strategy.custom_agents}
        stale_generated = (names - planned_names) & known
        if report_stale:
            for name in sorted(stale_generated):
                problems.append(
                    ("warn", f"{name}.md no esta en el plan actual; borralo si no lo usas")
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
        for target in sorted(names - {"orquestador"}):
            if target in stale_generated:
                continue
            if target not in allowed:
                problems.append(
                    ("warn", f"orquestador.md: no puede delegar en '{target}' (revisa sus permisos)")
                )

    for name, front in fronts.items():
        for secret in secret_paths[:20]:
            for action in ("read", "edit"):
                if permission_effect(front, action, secret) != "deny":
                    problems.append(
                        ("warn", f"{name}.md: no deniega {action} de '{secret}'")
                    )
        if name == "orquestador":
            continue
        if permission_effect(front, "subagent", "*") == "allow":
            problems.append(("warn", f"{name}.md: permite subagent '*'; conviene cerrar la lista"))
        if permission_effect(front, "external_directory", "*") != "deny":
            problems.append(
                ("warn", f"{name}.md: no deniega external_directory; puede salir del proyecto")
            )

    cfg = config_file(scope, project_dir)
    if cfg.exists() and "default_agent" not in cfg.read_text(encoding="utf-8"):
        problems.append(("warn", f"{cfg.name}: no define 'default_agent'"))

    if strategy is not None and strategy.add_local and strategy.local_model and catalog is not None:
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