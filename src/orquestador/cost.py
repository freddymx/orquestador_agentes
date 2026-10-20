"""Estimacion de coste: cuanto cuesta un plan frente a hacerlo todo con el modelo caro.

El reparto (`mix`) describe que parte del trabajo lleva cada rol. Hay **un unico
presupuesto de tokens por sesion** (`SESSION_TOKEN_BUDGET`) que se reparte segun ese
mix; si un rol no existe en el plan, su parte se redistribuye entre los presentes en
vez de desaparecer. Asi el coste con orquestacion y el de referencia cubren el mismo
trabajo y el ahorro no se infla.

Los precios salen de models.dev (la cache que mantiene OpenCode) cuando el modelo
aparece alli; si no, del catalogo embebido o de un valor supuesto marcado como tal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, ModelInfo, is_chat_model, price_of
from .planner import AgentPlan, OrchestrationPlan, _clean

# Tokens de una sesion tipica (ida y vuelta completa). Es una heuristica: el objetivo
# es comparar escenarios con el mismo trabajo, no predecir la factura exacta.
SESSION_TOKEN_BUDGET = 100_000

# Reparto por defecto de la complejidad de las subtareas. Ajustable con --*-share.
DEFAULT_MIX = {"local": 0.50, "cheap": 0.30, "specialist": 0.15, "orchestrator": 0.05}

ROLE_ORDER = ("orchestrator", "local", "cheap", "specialist")
ROLE_BY_TIER = {"local": "local", "cheap": "cheap", "frontier": "specialist"}
ROLE_LABELS = {
    "orchestrator": "orquestador",
    "local": "local",
    "cheap": "barato",
    "specialist": "especialista",
}


@dataclass
class AgentCost:
    name: str
    role: str
    model: str | None
    price_per_million: float
    exact: bool
    share: float
    tokens: int
    cost: float
    local: bool = False
    price_source: str = ""


@dataclass
class CostEstimate:
    agents: list[AgentCost] = field(default_factory=list)
    total_cost: float = 0.0
    naive_cost: float = 0.0  # todo con el modelo del orquestador, sin delegar
    sessions: int = 1
    token_budget: int = SESSION_TOKEN_BUDGET
    notes: list[str] = field(default_factory=list)

    @property
    def savings(self) -> float:
        return max(self.naive_cost - self.total_cost, 0.0)

    @property
    def savings_ratio(self) -> float:
        if self.naive_cost <= 0:
            return 0.0
        return self.savings / self.naive_cost

    def as_dict(self) -> dict:
        return {
            "sessions": self.sessions,
            "token_budget": self.token_budget,
            "total_cost_usd": round(self.total_cost, 4),
            "naive_cost_usd": round(self.naive_cost, 4),
            "savings_usd": round(self.savings, 4),
            "savings_ratio": round(self.savings_ratio, 3),
            "notes": self.notes,
            "agents": [
                {
                    "name": a.name,
                    "role": a.role,
                    "model": a.model,
                    "local": a.local,
                    "price_per_million": round(a.price_per_million, 4),
                    "price_is_exact": a.exact,
                    "price_source": a.price_source,
                    "share": a.share,
                    "tokens": a.tokens,
                    "cost_usd": round(a.cost, 4),
                }
                for a in self.agents
            ],
        }


def estimate(
    plan: OrchestrationPlan,
    catalog: Catalog,
    sessions: int = 1,
    mix: dict[str, float] | None = None,
    token_budget: int | None = None,
) -> CostEstimate:
    """Estima el coste de `sessions` sesiones con el mismo presupuesto de tokens."""
    sessions = max(int(sessions), 1)
    budget_per_session = max(int(token_budget or SESSION_TOKEN_BUDGET), 1)
    budget = budget_per_session * sessions
    result = CostEstimate(sessions=sessions, token_budget=budget)

    resolved = _resolve_agents(plan, catalog, result.notes)
    if not resolved:
        result.notes.append("No hay ningun agente con modelo resoluble: no se puede estimar el coste.")
        return result

    shares = _effective_shares(mix or DEFAULT_MIX, set(resolved), result.notes)

    # Reparto de tokens por agente. El ultimo absorbe el redondeo para que la suma
    # sea exactamente el presupuesto y las dos cifras cubran el mismo trabajo.
    slots: list[tuple[str, AgentPlan, ModelInfo, float]] = []
    for role in ROLE_ORDER:
        items = resolved.get(role) or []
        if not items or role not in shares:
            continue
        per_agent = shares[role] / len(items)
        slots.extend((role, agent, model, per_agent) for agent, model in items)

    remaining = budget
    for index, (role, agent, model, share) in enumerate(slots):
        provider = catalog.providers.get(model.provider)
        pricing = price_of(model, provider)
        blended = 0.0 if pricing is None else pricing.input * 0.8 + pricing.output * 0.2
        local = provider is not None and provider.is_local
        tokens = max(remaining, 0) if index == len(slots) - 1 else int(round(budget * share))
        remaining -= tokens
        cost = blended / 1_000_000 * tokens
        result.agents.append(
            AgentCost(
                name=agent.name,
                role=agent.role,
                model=agent.model,
                price_per_million=blended,
                exact=bool(pricing and pricing.exact),
                share=share,
                tokens=tokens,
                cost=cost,
                local=local,
                price_source=pricing.source if pricing else "",
            )
        )
        if pricing is not None and not pricing.exact and not local:
            result.notes.append(
                f"{agent.name}: precio no conocido en models.dev, se estima "
                f"{blended:.2f} $/1M ({pricing.source})"
            )

    result.total_cost = sum(a.cost for a in result.agents)

    # Referencia: el mismo trabajo, entero, con el modelo del orquestador.
    orchestrator_items = resolved.get("orchestrator") or []
    if orchestrator_items:
        orch_model = orchestrator_items[0][1]
        provider = catalog.providers.get(orch_model.provider)
        pricing = price_of(orch_model, provider)
        naive_price = 0.0 if pricing is None else pricing.input * 0.8 + pricing.output * 0.2
        result.naive_cost = naive_price / 1_000_000 * budget
    else:
        result.notes.append(
            "Sin modelo de orquestador resoluble no se puede calcular el coste de referencia"
        )

    _add_notes(result)
    return result


def _resolve_agents(
    plan: OrchestrationPlan, catalog: Catalog, notes: list[str]
) -> dict[str, list[tuple[AgentPlan, ModelInfo]]]:
    grouped: dict[str, list[tuple[AgentPlan, ModelInfo]]] = {}
    for agent in plan.agents:
        role = "orchestrator" if agent.role == "primary" else ROLE_BY_TIER.get(agent.tier, "specialist")
        model = _resolve(catalog, agent.model)
        if model is None:
            if agent.model:
                notes.append(f"{agent.name}: modelo '{agent.model}' no resoluble, se omite del reparto")
            continue
        grouped.setdefault(role, []).append((agent, model))
    return grouped


def _effective_shares(
    mix: dict[str, float], present: set[str], notes: list[str]
) -> dict[str, float]:
    """Normaliza el mix y reparte la parte de los roles que no existen."""
    raw = {role: max(float(mix.get(role, 0.0)), 0.0) for role in ROLE_ORDER}
    active = {role: value for role, value in raw.items() if value > 0 and role in present}
    dropped = {role: value for role, value in raw.items() if value > 0 and role not in present}

    if not active:
        if not present:
            return {}
        notes.append("El reparto no asigna trabajo a ningun agente presente: se reparte a partes iguales.")
        return {role: 1.0 / len(present) for role in present}

    total = sum(active.values())
    for role, value in dropped.items():
        notes.append(
            f"Sin agente {ROLE_LABELS.get(role, role)}: su {value * 100:.0f}% del trabajo "
            "se reparte entre los que si existen."
        )
    return {role: value / total for role, value in active.items()}


def _add_notes(result: CostEstimate) -> None:
    if result.agents and all(a.local for a in result.agents):
        result.notes.append(
            "Todos los agentes usan modelos locales: el coste es 0 y la ganancia no es de dinero, "
            "es de privacidad (nada sale de tu red)."
        )
    if result.naive_cost <= 0 and result.total_cost > 0:
        result.notes.append(
            "El orquestador es local, asi que no hay referencia de coste: el plan gasta "
            f"${result.total_cost:.4f} en los subagentes cloud."
        )
    if result.naive_cost > 0 and result.total_cost > result.naive_cost:
        result.notes.append(
            "El plan cuesta mas que hacerlo todo con el orquestador: revisa el reparto "
            "(--local-share/--cheap-share) o usa un modelo mas barato para el especialista."
        )
    if any(not a.exact and not a.local for a in result.agents):
        result.notes.append(
            "Hay precios estimados: models.dev no publica tarifa para esos modelos, "
            "asi que el ahorro es aproximado."
        )
    if not result.notes:
        result.notes.append(
            "Precios por 1M tokens de models.dev; el reparto de trabajo es una heuristica."
        )


def _resolve(catalog: Catalog, ref: str | None) -> ModelInfo | None:
    if not ref:
        return None
    clean = _clean(ref)
    if clean is None:
        return None
    model = catalog.find(clean.split("#", 1)[0])
    if model is not None:
        return model
    # Puede que el usuario haya escrito un id sin provider.
    return next((m for m in catalog.models if m.model_id == clean and is_chat_model(m.model_id)), None)
