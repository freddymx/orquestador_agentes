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

from .catalog import Catalog, ModelInfo, guess_tier
from .scan import ProjectProfile

TIER_LABELS = {"frontier": "frontera", "cheap": "barato", "local": "local"}


# --------------------------------------------------------------------------- #
# Opciones de configuracion
# --------------------------------------------------------------------------- #


@dataclass
class Strategy:
    orchestrator_model: str | None = None
    orchestrator_variant: str = "high"
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
    import yaml

    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def load_strategy(path: Path | None) -> Strategy:
    if path is None or not path.exists():
        return Strategy()
    return strategy_from_mapping(_load_yaml(path.read_text(encoding="utf-8")))


AUTO_VALUES = (None, "", "auto", "none", "null", "—", "-")


def _clean(value) -> str | None:
    """Normaliza centinelas ('auto', '', null) a None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in AUTO_VALUES else text


def strategy_from_mapping(data: dict) -> Strategy:
    strategy = Strategy()
    if not isinstance(data, dict):
        return strategy
    strategy.orchestrator_model = _clean(data.get("orchestrator_model") or data.get("orquestador_model"))
    strategy.orchestrator_variant = data.get("orchestrator_variant") or strategy.orchestrator_variant
    strategy.default_agent = bool(data.get("default_agent", True))
    strategy.scope = data.get("scope") or strategy.scope
    strategy.extra_rules = [str(r) for r in (data.get("rules") or [])]
    subagents = data.get("subagents") or {}
    if isinstance(subagents, dict):
        local = subagents.get("local") or {}
        cheap = subagents.get("barato") or {}
        spec = subagents.get("especialista") or subagents.get("specialist") or {}
        if isinstance(local, dict):
            strategy.local_model = _clean(local.get("model"))
            strategy.local_steps = int(local.get("steps", strategy.local_steps))
        if isinstance(cheap, dict):
            strategy.cheap_model = _clean(cheap.get("model"))
            strategy.cheap_steps = int(cheap.get("steps", strategy.cheap_steps))
        if isinstance(spec, dict):
            strategy.specialist_model = _clean(spec.get("model"))
            strategy.specialist_steps = int(spec.get("steps", strategy.specialist_steps))
            strategy.add_specialist = bool(spec.get("enabled", strategy.add_specialist))
    return strategy


def save_strategy(path: Path, strategy: Strategy, project: str) -> str:
    lines = [
        f"# Estrategia generada por orquestador para: {project}",
        "# Edita y vuelve a ejecutar: orq generate --strategy <este fichero>",
        f"orchestrator_model: {strategy.orchestrator_model or 'auto'}",
        f"orchestrator_variant: {strategy.orchestrator_variant}",
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
            lines.append(f'  - "{rule}"')
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
    variant = (strategy.orchestrator_variant or "").strip()
    if orchestrator_ref and variant and variant.lower() not in ("none", "default"):
        orchestrator_ref = f"{orchestrator_ref}#{variant}"

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