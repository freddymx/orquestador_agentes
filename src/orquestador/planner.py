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

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .catalog import Catalog, ModelInfo, guess_tier, meta_for
from .scan import ProjectProfile

TIER_LABELS = {"frontier": "frontera", "cheap": "barato", "local": "local"}


class StrategyError(ValueError):
    """Estrategia ilegible: el CLI lo convierte en un error con mensaje, sin traceback."""


# --------------------------------------------------------------------------- #
# Opciones de configuracion
# --------------------------------------------------------------------------- #


@dataclass
class Strategy:
    orchestrator_model: str | None = None
    orchestrator_variant: str | None = None
    local_model: str | None = None
    cheap_model: str | None = None
    specialist_model: str | None = None
    add_specialist: bool = True
    local_steps: int = 40
    cheap_steps: int = 12
    specialist_steps: int = 30
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


def strategy_from_mapping(data: dict) -> Strategy:
    strategy = Strategy()
    if not isinstance(data, dict):
        return strategy
    strategy.orchestrator_model = _clean(data.get("orchestrator_model") or data.get("orquestador_model"))
    strategy.orchestrator_variant = _variant(data.get("orchestrator_variant"))
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
        if isinstance(cheap, dict):
            strategy.cheap_model = _clean(cheap.get("model"))
            strategy.cheap_steps = _steps(cheap.get("steps"), strategy.cheap_steps, "subagents.barato")
        if isinstance(spec, dict):
            strategy.specialist_model = _clean(spec.get("model"))
            strategy.specialist_steps = _steps(
                spec.get("steps"), strategy.specialist_steps, "subagents.especialista"
            )
            strategy.add_specialist = bool(spec.get("enabled", strategy.add_specialist))
    return strategy


def _yaml_string(text: str) -> str:
    """Escalar YAML entre comillas, seguro ante comillas y saltos de linea."""
    import json

    return json.dumps(text, ensure_ascii=False)


def save_strategy(path: Path, strategy: Strategy, project: str) -> str:
    safe_project = " ".join(str(project).splitlines())
    lines = [
        f"# Estrategia generada por orquestador para: {safe_project}",
        "# Edita y vuelve a ejecutar: orq generate --strategy <este fichero>",
        f"orchestrator_model: {strategy.orchestrator_model or 'auto'}",
        f"orchestrator_variant: {strategy.orchestrator_variant or 'auto'}",
        f"default_agent: {str(strategy.default_agent).lower()}",
        f"scope: {strategy.scope}",
        "subagents:",
    ]
    for name, model, steps in (
        ("local", strategy.local_model, strategy.local_steps),
        ("barato", strategy.cheap_model, strategy.cheap_steps),
        ("especialista", strategy.specialist_model, strategy.specialist_steps),
    ):
        lines.append(f"  {name}:")
        lines.append(f"    model: {model or 'auto'}")
        lines.append(f"    steps: {steps}")
    if strategy.extra_rules:
        lines.append("rules:")
        for rule in strategy.extra_rules:
            lines.append(f"  - {_yaml_string(rule)}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- #
# Seleccion de modelos
# --------------------------------------------------------------------------- #


def pick_model(catalog: Catalog, tier: str, exclude: set[str] | None = None) -> ModelInfo | None:
    exclude = exclude or set()
    candidates = [m for m in catalog.by_tier(tier) if m.ref not in exclude]
    if not candidates and tier == "cheap":
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
    base_ref, _, inline_variant = (orchestrator_ref or "").partition("#")
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

    base_ref = _clean(base_ref)
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

    if strategy.local_model:
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

    if strategy.cheap_model:
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

    if strategy.add_specialist:
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

    plan = OrchestrationPlan(
        project=profile.name,
        profile=profile,
        strategy=strategy,
        agents=agents,
        notes=notes,
    )
    plan.provider_patches = provider_patches(catalog, strategy)
    return plan


def provider_patches(catalog: Catalog, strategy: Strategy) -> list[dict]:
    """Bloques de provider listos para pegar en opencode.jsonc si faltan."""
    patches: list[dict] = []
    for agent_model in (strategy.local_model,):
        if not agent_model:
            continue
        provider_name = agent_model.split("/", 1)[0]
        provider = catalog.providers.get(provider_name)
        if provider is None or not provider.base_url:
            continue
        if provider.declared_models:
            continue
        model_id = agent_model.split("/", 1)[1]
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