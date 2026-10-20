"""Planificacion de agentes: decide que subagentes hacen falta y con que modelo.

Fichero de estrategia (opcional) con extension .orquestador.yaml en el proyecto:

    project: mi-app
    orchestrator_model: openai/gpt-5#high
    subagents:
      barato:
        model: openai/gpt-6-luna#low
        steps: 12
    rules:
      - "Usa el agente barato para renombrados"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .catalog import Catalog, ModelInfo, guess_tier, meta_for
from .fsutil import atomic_write_text, backup_file
from .scan import ProjectProfile

TIER_LABELS = {"frontier": "frontera", "cheap": "barato", "local": "local"}
PRESETS = ("balanced", "lean", "strict")
CUSTOM_TIERS = ("local", "cheap", "frontier")
CUSTOM_PERMISSIONS = ("read", "edit", "full")
RESERVED_AGENTS = {"orquestador", "local", "barato", "especialista", "specialist"}
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
SAFE_SCALAR = re.compile(r"^[A-Za-z0-9._:/@+-]+(?:#[A-Za-z0-9._-]+)?$")


def yaml_scalar(value: object) -> str:
    """Escalar YAML seguro: deja intactos los valores normales y cita el resto.

    Los model refs vienen de configuracion o de un endpoint HTTP; no son de fiar.
    Un salto de linea o una comilla no puede inyectar claves en el YAML generado.
    """
    import json

    text = str(value)
    if SAFE_SCALAR.fullmatch(text):
        return text
    return json.dumps(text, ensure_ascii=False)


class StrategyError(ValueError):
    """Estrategia ilegible: el CLI lo convierte en un error con mensaje, sin traceback."""


# --------------------------------------------------------------------------- #
# Opciones de configuracion
# --------------------------------------------------------------------------- #


@dataclass
class CustomAgent:
    """Subagente definido por el usuario, ademas de los tres roles fijos."""

    name: str
    model: str | None = None  # None / auto: se elige por tier
    steps: int = 20
    enabled: bool = True
    tier: str | None = None  # local | cheap | frontier; si falta se deduce
    permissions: str = "edit"  # read | edit | full
    description: str = ""
    prompt: str = ""
    color: str = "#9d7bff"


@dataclass
class Strategy:
    orchestrator_model: str | None = None
    orchestrator_variant: str | None = None
    local_model: str | None = None
    cheap_model: str | None = None
    specialist_model: str | None = None
    add_local: bool = True
    add_cheap: bool = True
    add_specialist: bool = True
    local_steps: int = 40
    cheap_steps: int = 12
    specialist_steps: int = 30
    preset: str = "balanced"  # balanced | lean | strict
    custom_agents: list[CustomAgent] = field(default_factory=list)
    extra_rules: list[str] = field(default_factory=list)
    default_agent: bool = True
    scope: str = "project"  # project | global


def _load_yaml(text: str) -> dict:
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def load_strategy(path: Path | None) -> Strategy:
    if path is None or not path.exists():
        return Strategy()
    try:
        data = _load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise StrategyError(f"no se pudo leer {path.name}: {exc}") from None
    return strategy_from_mapping(data)


AUTO_VALUES = (None, "", "auto", "none", "null", "—", "-")


def _clean(value) -> str | None:
    """Normaliza centinelas ('auto', '', null) a None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in AUTO_VALUES else text


def _steps(value, default: int, field_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise StrategyError(f"{field_name}: 'steps' debe ser un entero positivo (recibido: {value!r})")
    try:
        steps = int(value)
    except (TypeError, ValueError):
        raise StrategyError(
            f"{field_name}: 'steps' debe ser un entero positivo (valor recibido: {value!r})"
        ) from None
    if steps <= 0:
        raise StrategyError(f"{field_name}: 'steps' debe ser mayor que 0 (valor recibido: {steps})")
    return steps


def _variant(value) -> str | None:
    """None = automatico; 'none' = sin variante; otro = variante explicita."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "auto", "null", "—", "-"):
        return None
    if text in ("none", "default"):
        return "none"
    return text


def _rules(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _enabled(value, default: bool = True) -> bool:
    """`enabled: false` desactiva un subagente; ausente o true lo mantiene."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "off", "0")


def normalize_preset(value) -> str:
    """balanced | lean | strict. `auto`/vacio equivalen a balanced."""
    if value is None:
        return "balanced"
    text = str(value).strip().lower()
    if text in ("", "auto", "none", "null", "default", "—", "-"):
        return "balanced"
    if text not in PRESETS:
        raise StrategyError(
            f"preset debe ser uno de {', '.join(PRESETS)} (recibido: {value!r})"
        )
    return text


def _color(value) -> str:
    text = "#9d7bff" if value is None or value == "" else str(value).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise StrategyError(f"color debe ser un hex #rrggbb (recibido: {value!r})")
    return text


def _custom_agent(name: str, spec: dict) -> CustomAgent:
    field_name = f"subagents.{name}"
    raw_model = spec.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise StrategyError(f"{field_name}: 'model' debe ser texto (recibido: {raw_model!r})")
    raw_tier = spec.get("tier")
    tier = str(raw_tier).strip().lower() if raw_tier not in (None, "") else None
    if tier in ("auto", "none", "null", "—", "-"):
        tier = None
    if tier is not None and tier not in CUSTOM_TIERS:
        raise StrategyError(
            f"{field_name}: 'tier' debe ser {', '.join(CUSTOM_TIERS)} (recibido: {raw_tier!r})"
        )
    raw_permissions = spec.get("permissions")
    permissions = str(raw_permissions).strip().lower() if raw_permissions not in (None, "") else "edit"
    if permissions not in CUSTOM_PERMISSIONS:
        raise StrategyError(
            f"{field_name}: 'permissions' debe ser "
            f"{', '.join(CUSTOM_PERMISSIONS)} (recibido: {raw_permissions!r})"
        )
    for key, raw in (("description", spec.get("description")), ("prompt", spec.get("prompt"))):
        if raw is not None and not isinstance(raw, str):
            raise StrategyError(f"{field_name}: '{key}' debe ser texto (recibido: {raw!r})")
    return CustomAgent(
        name=name,
        model=_clean(raw_model),
        steps=_steps(spec.get("steps"), 20, field_name),
        enabled=_enabled(spec.get("enabled"), True),
        tier=tier,
        permissions=permissions,
        description=str(spec.get("description") or "").strip(),
        prompt=str(spec.get("prompt") or "").strip(),
        color=_color(spec.get("color")),
    )


def _custom_agents(subagents: dict) -> list[CustomAgent]:
    fixed = {"local", "barato", "especialista", "specialist"}
    seen: set[str] = set()
    custom: list[CustomAgent] = []
    for raw_name, spec in subagents.items():
        name = str(raw_name).strip().lower()
        if name in fixed:
            continue
        if not AGENT_NAME.fullmatch(name) or name in RESERVED_AGENTS:
            raise StrategyError(
                f"subagents.{raw_name}: nombre invalido. Usa minusculas, empieza por letra "
                f"y solo [a-z0-9_-] (max. 40); reservados: {', '.join(sorted(RESERVED_AGENTS))}"
            )
        if name in seen:
            raise StrategyError(f"subagents.{raw_name}: nombre duplicado tras normalizar")
        if not isinstance(spec, dict):
            raise StrategyError(f"subagents.{name}: debe ser un mapa con model/steps/...")
        seen.add(name)
        custom.append(_custom_agent(name, spec))
    return custom


def strategy_from_mapping(data: dict) -> Strategy:
    strategy = Strategy()
    if not isinstance(data, dict):
        return strategy
    strategy.orchestrator_model = _clean(data.get("orchestrator_model") or data.get("orquestador_model"))
    strategy.orchestrator_variant = _variant(data.get("orchestrator_variant"))
    strategy.preset = normalize_preset(data.get("preset"))
    strategy.default_agent = bool(data.get("default_agent", True))
    strategy.scope = str(data.get("scope") or strategy.scope).strip().lower()
    strategy.extra_rules = _rules(data.get("rules"))
    subagents = data.get("subagents") or {}
    if isinstance(subagents, dict):
        local = subagents.get("local") or {}
        cheap = subagents.get("barato") or {}
        spec = subagents.get("especialista") or subagents.get("specialist") or {}
        if isinstance(local, dict):
            strategy.local_model = _clean(local.get("model"))
            strategy.local_steps = _steps(local.get("steps"), strategy.local_steps, "subagents.local")
            strategy.add_local = _enabled(local.get("enabled"), strategy.add_local)
        if isinstance(cheap, dict):
            strategy.cheap_model = _clean(cheap.get("model"))
            strategy.cheap_steps = _steps(cheap.get("steps"), strategy.cheap_steps, "subagents.barato")
            strategy.add_cheap = _enabled(cheap.get("enabled"), strategy.add_cheap)
        if isinstance(spec, dict):
            strategy.specialist_model = _clean(spec.get("model"))
            strategy.specialist_steps = _steps(
                spec.get("steps"), strategy.specialist_steps, "subagents.especialista"
            )
            strategy.add_specialist = _enabled(spec.get("enabled"), strategy.add_specialist)
        strategy.custom_agents = _custom_agents(subagents)
    return strategy


def _yaml_string(text: str) -> str:
    """Escalar YAML entre comillas, seguro ante comillas y saltos de linea."""
    import json

    return json.dumps(text, ensure_ascii=False)


def strategy_text(strategy: Strategy, project: str) -> str:
    """Contenido exacto de `.orquestador.yaml` (sin tocar disco)."""
    safe_project = " ".join(str(project).splitlines())
    preset = normalize_preset(strategy.preset)
    lines = [
        f"# Estrategia generada por orquestador para: {safe_project}",
        "# Edita y vuelve a ejecutar: orq generate --strategy <este fichero>",
        f"orchestrator_model: {yaml_scalar(strategy.orchestrator_model or 'auto')}",
        f"orchestrator_variant: {yaml_scalar(strategy.orchestrator_variant or 'auto')}",
        "# preset: balanced (equilibrado) | lean (un solo trabajador) | strict (sin shell)",
        f"preset: {preset}",
        f"default_agent: {str(strategy.default_agent).lower()}",
        f"scope: {yaml_scalar(strategy.scope)}",
        "subagents:",
    ]
    for name, model, steps, enabled in (
        ("local", strategy.local_model, strategy.local_steps, strategy.add_local),
        ("barato", strategy.cheap_model, strategy.cheap_steps, strategy.add_cheap),
        ("especialista", strategy.specialist_model, strategy.specialist_steps, strategy.add_specialist),
    ):
        lines.append(f"  {name}:")
        lines.append(f"    model: {yaml_scalar(model or 'auto')}")
        lines.append(f"    steps: {steps}")
        if not enabled:
            lines.append("    enabled: false")
    for custom in strategy.custom_agents:
        lines.append(f"  {custom.name}:")
        lines.append(f"    model: {yaml_scalar(custom.model or 'auto')}")
        lines.append(f"    steps: {custom.steps}")
        if not custom.enabled:
            lines.append("    enabled: false")
        if custom.tier:
            lines.append(f"    tier: {custom.tier}")
        if custom.permissions != "edit":
            lines.append(f"    permissions: {custom.permissions}")
        if custom.description:
            lines.append(f"    description: {_yaml_string(custom.description)}")
        if custom.prompt:
            lines.append(f"    prompt: {_yaml_string(custom.prompt)}")
        if custom.color != "#9d7bff":
            lines.append(f"    color: {_yaml_string(custom.color)}")
    if strategy.extra_rules:
        lines.append("rules:")
        for rule in strategy.extra_rules:
            lines.append(f"  - {_yaml_string(rule)}")
    return "\n".join(lines) + "\n"


def save_strategy(path: Path, strategy: Strategy, project: str, backup: bool = False) -> str:
    text = strategy_text(strategy, project)
    if backup:
        backup_file(path)
    atomic_write_text(path, text)
    return text


# --------------------------------------------------------------------------- #
# Seleccion de modelos
# --------------------------------------------------------------------------- #


def pick_model(
    catalog: Catalog,
    tier: str,
    exclude: set[str] | None = None,
    fallback: bool = True,
) -> ModelInfo | None:
    exclude = exclude or set()
    candidates = [m for m in catalog.by_tier(tier) if m.ref not in exclude]
    if not candidates and fallback and tier == "cheap":
        candidates = [m for m in catalog.by_tier("frontier") if m.ref not in exclude]
    if not candidates:
        return None
    # Preferimos modelos con tool-calling declarado y mas grandes para frontier.
    def score(model: ModelInfo) -> tuple:
        params = model.params_b or 0
        tools = 1 if model.supports_tools else 0
        return (tools, params if tier == "frontier" else -params)

    return sorted(candidates, key=score, reverse=True)[0]


def auto_strategy(catalog: Catalog, profile: ProjectProfile) -> tuple[Strategy, list[str]]:
    """Elige modelos por defecto y devuelve las notas explicativas."""
    strategy = Strategy()
    notes: list[str] = []

    frontier = pick_model(catalog, "frontier")
    cheap = pick_model(catalog, "cheap", exclude={frontier.ref} if frontier else set())
    local = pick_model(catalog, "local")

    if frontier:
        strategy.orchestrator_model = frontier.ref
        strategy.specialist_model = frontier.ref
        notes.append(f"Orquestador y especialista: {frontier.ref} (tier frontera)")
    if cheap:
        strategy.cheap_model = cheap.ref
        notes.append(f"Agente barato: {cheap.ref}")
    if local:
        strategy.local_model = local.ref
        notes.append(f"Agente local: {local.ref} (privado, coste cero)")
    if not cheap and not local:
        strategy.add_specialist = True
        notes.append(
            "Solo hay un tier disponible: se generara orquestador + especialista con el mismo modelo"
        )
    if not local:
        notes.append("Sin modelos locales detectados: el agente local no se generara")
    return strategy, notes


# --------------------------------------------------------------------------- #
# Plan de agentes
# --------------------------------------------------------------------------- #


@dataclass
class AgentPlan:
    name: str
    role: str  # primary | subagent
    tier: str
    model: str | None
    steps: int | None = None
    color: str = "#ffffff"
    reason: str = ""
    custom: CustomAgent | None = None


@dataclass
class OrchestrationPlan:
    project: str
    profile: ProjectProfile
    strategy: Strategy
    agents: list[AgentPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    provider_patches: list[dict] = field(default_factory=list)

    @property
    def orchestrator(self) -> AgentPlan | None:
        return next((a for a in self.agents if a.role == "primary"), None)

    @property
    def subagents(self) -> list[AgentPlan]:
        return [a for a in self.agents if a.role == "subagent"]

    @property
    def delegation_targets(self) -> list[str]:
        return [a.name for a in self.subagents]


def _base_model(ref: str | None) -> str | None:
    """Quita el sufijo #variant (el especialista no hereda el esfuerzo del orquestador)."""
    return ref.split("#", 1)[0] if ref else None


def build_plan(profile: ProjectProfile, catalog: Catalog, strategy: Strategy | None = None) -> OrchestrationPlan:
    strategy = strategy or Strategy()
    strategy.preset = normalize_preset(strategy.preset)
    strategy.orchestrator_model = _clean(strategy.orchestrator_model)
    strategy.cheap_model = _clean(strategy.cheap_model)
    strategy.local_model = _clean(strategy.local_model)
    strategy.specialist_model = _clean(strategy.specialist_model)
    notes: list[str] = []
    agents: list[AgentPlan] = []

    needs_auto = any(
        value is None for value in (strategy.orchestrator_model, strategy.cheap_model, strategy.local_model)
    )
    if needs_auto:
        auto, auto_notes = auto_strategy(catalog, profile)
        notes.extend(auto_notes)
        for attr in ("orchestrator_model", "local_model", "cheap_model", "specialist_model"):
            if getattr(strategy, attr) is None:
                setattr(strategy, attr, getattr(auto, attr))

    # Sin modelo de orquestador el agente heredaria el de la sesion padre, que es
    # justo el gasto que esta herramienta existe para evitar. Mejor un modelo local
    # que un orquestador sin fijar.
    if strategy.orchestrator_model is None:
        fallback = catalog.models[0] if catalog.models else None
        if fallback is not None:
            strategy.orchestrator_model = fallback.ref
            notes.append(
                f"No hay modelos de frontera ni baratos: el orquestador usa {fallback.ref} "
                "(unico disponible). Anade un proveedor cloud en opencode.jsonc para repartir mejor."
            )
        else:
            notes.append(
                "No hay NINGUN modelo disponible: define uno en opencode.jsonc "
                "o pasa --endpoint http://host:puerto/v1"
            )

    orchestrator_ref = strategy.orchestrator_model
    raw_ref, _, inline_variant = (orchestrator_ref or "").partition("#")
    raw_variant = (strategy.orchestrator_variant or "").strip().lower()
    disable_variant = raw_variant in ("none", "default")
    requested = None if disable_variant else (_clean(strategy.orchestrator_variant) or _clean(inline_variant))
    if requested:
        requested = requested.lower()  # las variantes de models.dev son minusculas
    if requested and inline_variant and _clean(inline_variant) not in (None, requested):
        notes.append(
            f"La estrategia pide la variante '{requested}' y el modelo ya lleva '#{inline_variant}': "
            f"se usa '{requested}'."
        )

    base_ref = _clean(raw_ref) or None
    meta = meta_for(base_ref) if base_ref else None
    variant = requested
    if variant is None and not disable_variant and meta is not None:
        # Automatico: solo el esfuerzo alto, y solo si el modelo lo declara.
        if meta.variants_known and "high" in meta.variants:
            variant = "high"

    # Solo se anade #variante si el modelo la declara en models.dev: una variante
    # desconocida es un error de resolucion de modelo en OpenCode, no un aviso.
    orchestrator_ref = base_ref
    if base_ref and variant:
        if meta is None:
            orchestrator_ref = f"{base_ref}#{variant}"
            notes.append(
                f"No se pudo verificar la variante '{variant}' de {base_ref}: se aplica tal cual."
            )
        elif not meta.variants_known:
            notes.append(
                f"{base_ref} declara variantes que no se pueden enumerar: se omite '#{variant}'."
            )
        elif variant in meta.variants:
            orchestrator_ref = f"{base_ref}#{variant}"
        else:
            disponibles = ", ".join(meta.variants) or "ninguna"
            notes.append(
                f"Variante '{variant}' no disponible en {base_ref} (disponibles: {disponibles}): "
                "se omite."
            )

    # La estrategia guardada refleja lo que se ha aplicado de verdad.
    strategy.orchestrator_model = base_ref
    if disable_variant:
        strategy.orchestrator_variant = "none"
    else:
        strategy.orchestrator_variant = requested or "auto"

    agents.append(
        AgentPlan(
            name="orquestador",
            role="primary",
            tier="frontier",
            model=orchestrator_ref,
            color="#fff833",
            reason="Decide, descompone y delega. Sin permisos de escritura: su contexto es el mas caro.",
        )
    )

    add_local, add_cheap, add_specialist = (
        strategy.add_local, strategy.add_cheap, strategy.add_specialist
    )
    if strategy.preset == "lean":
        # Un solo trabajador: local (privado/gratis) > barato > especialista.
        candidates: list[str] = []
        if add_local and strategy.local_model:
            candidates.append("local")
        if add_cheap and strategy.cheap_model:
            candidates.append("barato")
        if add_specialist:
            candidates.append("especialista")
        chosen = candidates[0] if candidates else None
        add_local = chosen == "local"
        add_cheap = chosen == "barato"
        add_specialist = chosen == "especialista"
        if chosen:
            notes.append(
                f"Preset lean: se genera un solo trabajador ('{chosen}') para reducir "
                "contexto y coste; los demas roles fijos quedan fuera."
            )
        else:
            notes.append(
                "Preset lean: no hay ningun trabajador disponible; solo se genera el orquestador."
            )
        enabled_custom = [c for c in strategy.custom_agents if c.enabled]
        if enabled_custom:
            notes.append(
                f"Preset lean: se omiten {len(enabled_custom)} subagente(s) personalizado(s). "
                "Usa balanced para incluirlos."
            )
    elif strategy.preset == "strict":
        notes.append(
            "Preset strict: los subagentes no tienen shell y las ediciones cloud piden "
            "confirmacion; el orquestador tampoco puede navegar."
        )

    if not add_local:
        notes.append(
            "Agente local desactivado en la estrategia."
            if not strategy.add_local
            else "Preset lean: se omite el agente local."
        )
    elif strategy.local_model:
        agents.append(
            AgentPlan(
                name="local",
                role="subagent",
                tier="local",
                model=strategy.local_model,
                steps=strategy.local_steps,
                color="#ff8236",
                reason="Trabajo mecanico sobre ficheros, gratis y sin salir de la maquina.",
            )
        )
    else:
        notes.append("Se omite el agente local: no hay modelos locales configurados.")

    if not add_cheap:
        notes.append(
            "Agente barato desactivado en la estrategia."
            if not strategy.add_cheap
            else "Preset lean: se omite el agente barato."
        )
    elif strategy.cheap_model:
        agents.append(
            AgentPlan(
                name="barato",
                role="subagent",
                tier="cheap",
                model=strategy.cheap_model,
                steps=strategy.cheap_steps,
                color="#0c8c0c",
                reason="Tareas mecanicas en la nube, mas rapido y fiable que local, coste bajo.",
            )
        )
    else:
        notes.append("Se omite el agente barato: no hay modelos economicos detectados.")

    if add_specialist:
        agents.append(
            AgentPlan(
                name="especialista",
                role="subagent",
                tier="frontier",
                model=_clean(strategy.specialist_model) or _base_model(orchestrator_ref),
                steps=strategy.specialist_steps,
                color="#4ea8ff",
                reason="Trabajo profundo de dominio: puede editar y decidir, con contexto limpio.",
            )
        )

    if strategy.preset != "lean":
        for custom in strategy.custom_agents:
            if not custom.enabled:
                notes.append(f"Subagente personalizado '{custom.name}' desactivado en la estrategia.")
                continue
            model = _clean(custom.model)
            if model is None:
                wanted = custom.tier or "cheap"
                # Un tier explicito no se cambia por otro: pedir `local` y acabar en la
                # nube rompe la promesa de privacidad. Solo el modo sin tier prueba local.
                picked = pick_model(catalog, wanted, fallback=custom.tier is None)
                if picked is None and custom.tier is None:
                    picked = pick_model(catalog, "local")
                if picked is None:
                    notes.append(
                        f"Se omite el subagente '{custom.name}': no hay modelos para elegir "
                        f"automaticamente (tier {wanted})."
                    )
                    continue
                model = picked.ref
                notes.append(
                    f"Subagente '{custom.name}': modelo automatico {model}."
                )
            tier = custom.tier or tier_of(catalog, model.split("#", 1)[0])
            if tier == "desconocido":
                tier = "cheap"
            agents.append(
                AgentPlan(
                    name=custom.name,
                    role="subagent",
                    tier=tier,
                    model=model,
                    steps=custom.steps,
                    color=custom.color,
                    reason=custom.description or f"Subagente personalizado '{custom.name}'.",
                    custom=custom,
                )
            )

    plan = OrchestrationPlan(
        project=profile.name,
        profile=profile,
        strategy=strategy,
        agents=agents,
        notes=notes,
    )
    plan.provider_patches = provider_patches(catalog, strategy, plan.agents)
    return plan


def provider_patches(
    catalog: Catalog,
    strategy: Strategy,
    agents: list[AgentPlan] | None = None,
) -> list[dict]:
    """Bloques de provider listos para pegar en opencode.jsonc si faltan."""
    patches: list[dict] = []
    candidates: list[str] = []
    if agents is not None:
        candidates = [a.model for a in agents if a.model and a.role == "subagent"]
    else:
        if strategy.add_local and strategy.local_model:
            candidates.append(strategy.local_model)
        if strategy.preset != "lean":
            for custom in strategy.custom_agents:
                if custom.enabled and custom.model:
                    candidates.append(custom.model)
    seen: set[tuple[str, str]] = set()
    for agent_model in candidates:
        ref = agent_model.split("#", 1)[0]
        if "/" not in ref:
            continue
        provider_name, model_id = ref.split("/", 1)
        provider = catalog.providers.get(provider_name)
        if provider is None or not provider.base_url:
            continue
        if provider.declared_models:
            continue
        key = (provider_name, model_id)
        if key in seen:
            continue
        seen.add(key)
        patches.append(
            {
                "provider": provider_name,
                "baseURL": provider.base_url,
                "model_id": model_id,
            }
        )
    return patches


def tier_of(catalog: Catalog, ref: str | None) -> str:
    if not ref:
        return "desconocido"
    model = catalog.find(ref)
    if model is None:
        return "desconocido"
    return guess_tier(model, catalog.providers.get(model.provider))