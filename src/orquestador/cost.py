"""Estimacion de coste: cuanto cuesta un plan frente a hacerlo todo con el modelo caro.

La idea es hacer tangible el argumento de la guia: si el 80% de los tokens acaba
en un modelo barato o local, la factura cambia de orden de magnitud.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, ModelInfo, blended_price, is_chat_model
from .planner import OrchestrationPlan, _clean

# Reparto por defecto de la complejidad de las subtareas. Ajustable.
DEFAULT_MIX = {"local": 0.50, "cheap": 0.30, "specialist": 0.15, "orchestrator": 0.05}

# Tokens medios por sesion tipica, por rol. Heuristicas: el objetivo es comparar
# escenarios, no predecir la factura exacta.
TOKENS_PER_SESSION = {
    "orchestrator": 12_000,
    "local": 25_000,
    "cheap": 18_000,
    "specialist": 45_000,
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


@dataclass
class CostEstimate:
    agents: list[AgentCost] = field(default_factory=list)
    total_cost: float = 0.0
    naive_cost: float = 0.0  # todo con el modelo del orquestador, sin delegar
    sessions: int = 1
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
                    "share": a.share,
                    "tokens": a.tokens,
                    "cost_usd": round(a.cost, 4),
                }
                for a in self.agents
            ],
        }


ROLE_BY_TIER = {"local": "local", "cheap": "cheap", "frontier": "specialist"}


def estimate(plan: OrchestrationPlan, catalog: Catalog, sessions: int = 1,
             mix: dict[str, float] | None = None) -> CostEstimate:
    mix = mix or DEFAULT_MIX
    result = CostEstimate(sessions=sessions)

    orchestrator = plan.orchestrator
    orch_model = _resolve(catalog, orchestrator.model if orchestrator else None)

    # El coste "naive" es lo que costaria si el padre hiciera el 100% del trabajo.
    if orch_model is not None:
        price = blended_price(orch_model, catalog.providers.get(orch_model.provider))
        total_tokens = sum(TOKENS_PER_SESSION.values()) * sessions
        result.naive_cost = price / 1_000_000 * total_tokens
    else:
        result.notes.append(
            "Sin modelo de orquestador resoluble no se puede calcular el coste de referencia"
        )

    for agent in plan.agents:
        role = "orchestrator" if agent.role == "primary" else ROLE_BY_TIER.get(agent.tier, "specialist")
        share = mix.get(role, 0.0)
        if share == 0:
            continue
        model = _resolve(catalog, agent.model)
        if model is None:
            if share:
                result.notes.append(f"{agent.name}: modelo '{agent.model}' no resoluble, se omite del reparto")
            continue
        provider = catalog.providers.get(model.provider)
        local = provider is not None and provider.is_local
        price = blended_price(model, provider)
        exact = price > 0 and _has_exact_price(model, provider)
        tokens = int(TOKENS_PER_SESSION.get(role, 20_000) * share * sessions)
        cost = price / 1_000_000 * tokens
        result.agents.append(
            AgentCost(
                name=agent.name,
                role=agent.role,
                model=agent.model,
                price_per_million=price,
                exact=exact,
                share=share,
                tokens=tokens,
                cost=cost,
                local=local,
            )
        )
        if not exact and not local:
            result.notes.append(
                f"{agent.name}: precio no conocido en el catalogo, se estima {price:.2f} $/1M"
            )

    result.total_cost = sum(a.cost for a in result.agents)
    local_agents = [a for a in result.agents if a.local]
    if result.agents and len(local_agents) == len(result.agents):
        result.notes.append(
            "Todos los agentes usan modelos locales: el coste es 0 y la ganancia no es de dinero, "
            "es de privacidad (nada sale de tu red)."
        )
    if result.naive_cost <= 0:
        result.notes.append(
            "El modelo del orquestador tambien es local, asi que no hay referencia de coste con que comparar."
        )
    if not result.notes:
        result.notes.append("Precios del catalogo embebido; no son cotizaciones en firme.")
    return result


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


def _has_exact_price(model: ModelInfo, provider) -> bool:
    from .catalog import known_price

    if provider is not None and provider.is_local:
        return True
    return known_price(model.model_id) is not None