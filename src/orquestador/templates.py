"""Plantillas de agentes OpenCode V2 y del bloque de configuracion.

Todo se genera a partir del perfil del proyecto y del plan, sin texto muerto:
si el proyecto no tiene tests, no se menciona el comando de test.
"""

from __future__ import annotations

from .catalog import TIER_HINTS  # noqa: F401  (reexportado por comodidad)
from .planner import AgentPlan, OrchestrationPlan
from .scan import ProjectProfile


def _frontmatter(plan: AgentPlan, description: str) -> list[str]:
    lines = ["---", f'description: "{description}"', f"mode: {plan.role}"]
    if plan.model:
        lines.append(f"model: {plan.model}")
    if plan.steps:
        lines.append(f"steps: {plan.steps}")
    lines.append(f'color: "{plan.color}"')
    return lines


def _deny_all(actions: list[str]) -> list[str]:
    lines = ["permissions:"]
    for action in actions:
        lines += [f"  - action: {action}", '    resource: "*"', "    effect: deny"]
    return lines


def _allow(action: str, resource: str) -> list[str]:
    return [f"  - action: {action}", f"    resource: {resource}", "    effect: allow"]


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #

ORCHESTRATOR_RULES = [
    "Nunca realices tareas tu mismo: tu unico trabajo es decidir y delegar.",
    "Antes de delegar, reduce la tarea a un objetivo verificable.",
    "Nunca copies contenido de ficheros en el prompt: pasa rutas y deja que los lea el subagente.",
    "Evita adjuntar ficheros con @fichero.md: eso carga tu contexto, que es el mas caro.",
    "Si dudas entre dos subagentes, elige el mas barato de los dos que pueda servir.",
    "Si dudas entre delegar y hacerlo tu, delega: tu contexto es mas caro.",
    "Lanza en paralelo las subtareas independientes.",
]


def _as_bullet(rule: str) -> str:
    """Evita '- -' si la regla ya venia con guion."""
    text = rule.strip()
    if text.startswith("- "):
        text = text[2:]
    return f"- {text}"


def orchestrator_prompt(plan: OrchestrationPlan) -> str:
    profile = plan.profile
    targets = plan.delegation_targets
    local = next((a for a in plan.subagents if a.tier == "local"), None)
    cheap = next((a for a in plan.subagents if a.tier == "cheap"), None)
    specialist = next((a for a in plan.subagents if a.tier == "frontier"), None)

    routing: list[str] = []
    if local:
        routing.append(
            f"- `{local.name}` ({local.model}): nano y small. Leer, buscar, editar en masa, refactors mecanicos."
        )
    if cheap:
        routing.append(
            f"- `{cheap.name}` ({cheap.model}): medium. Renombrados, formateo, redaccion simple, referencias."
        )
    if specialist:
        routing.append(
            f"- `{specialist.name}` ({specialist.model}): large. Diseño, cambios transversales, "
            "trabajo profundo de dominio. Delegasible aunque modifique codigo."
        )
    if not routing:
        routing.append("- No hay subagentes configurados: no puedes delegar. Revisa la configuracion.")

    context = [
        f"- **Proyecto**: `{profile.name}` ({profile.stack_label}).",
    ]
    if profile.key_dirs:
        dirs = ", ".join(f"`{d}/`" for d in profile.key_dirs[:8])
        context.append(f"- **Carpetas clave**: {dirs}.")
    if profile.languages:
        langs = ", ".join(f"{lang} ({n} ficheros)" for lang, n in list(profile.languages.items())[:5])
        context.append(f"- **Lenguajes**: {langs}.")
    if profile.test_command:
        context.append(
            f"- **Tests**: `{profile.test_command}`. Ningun cambio se da por bueno sin pasarlos."
        )
    if profile.lint_command:
        context.append(f"- **Lint/estilo**: `{profile.lint_command}`.")
    if profile.secret_paths:
        context.append(
            f"- **No tocar nunca**: {', '.join(f'`{p}`' for p in profile.secret_paths[:5])}."
        )
    if profile.is_monorepo:
        context.append("- Es un monorepo: localiza el paquete afectado antes de delegar.")

    rules = list(ORCHESTRATOR_RULES)
    if local and cheap:
        rules.append(f"- Si dudas entre `{local.name}` y `{cheap.name}`, elige `{local.name}`.")
    rules.extend(plan.strategy.extra_rules)

    body = [
        f"Eres el orquestador del proyecto `{profile.name}`. No escribes codigo: diriges.",
        "",
        "## Contexto del proyecto",
        *context,
        "",
        "## Objetivo",
        "",
        "Entender el problema (sin actuar, salvo razonar) y crear un TODO de subtareas pequenas y",
        "sencillas: divide y venceras. No realices las tareas aunque sean rapidas y faciles.",
        "",
        "## Asignar subtareas",
        "",
        "Cada subtarea la asignas a un subagente y le envies toda la informacion que necesite para",
        "resolverla: rutas, criterios de aceptacion y contexto minimo.",
        "",
        "## Enrutado",
        "",
        "Cada subtarea tiene una complejidad: nano, small, medium o large.",
        "",
        *routing,
        "",
        "## Reglas",
        "",
        *[_as_bullet(rule) for rule in rules],
        "",
        "## Ejecucion",
        "",
        "- Cada subagente devuelve un resumen corto. Si no basta, lanza una segunda pasada con una",
        "  pregunta concreta en lugar de leer los ficheros tu.",
        "- Si una decision es de arquitectura o de producto, pregunta al usuario antes de lanzarla.",
        "- Al usuario le hablas tu: que se ha hecho, que has decidido y que queda pendiente.",
    ]
    return "\n".join(body).rstrip() + "\n"


def orchestrator_agent(plan: OrchestrationPlan) -> str:
    primary = plan.orchestrator
    assert primary is not None
    description = (
        "Orquestador del proyecto. Descompone la peticion, decide que subagente hace cada parte "
        "y te responde a ti. Delega siempre: no escribe codigo directamente."
    )
    lines = _frontmatter(primary, description)
    lines += _deny_all(["edit", "write", "subagent", "shell"])
    for target in plan.delegation_targets:
        lines += _allow("subagent", target)
    lines.append("---")
    lines.append(orchestrator_prompt(plan))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Local
# --------------------------------------------------------------------------- #


def local_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    profile = plan.profile
    description = (
        "Trabajo sencillo y mecanico sobre los ficheros del proyecto con un modelo local. "
        "Usalo para leer, buscar y editar en masa sin que nada salga de este equipo."
    )
    lines = _frontmatter(agent, description)
    lines += _deny_all(["subagent", "webfetch", "websearch"])
    for secret in profile.secret_paths[:20]:
        lines += ["  - action: read", f'    resource: "{secret}"', "    effect: deny"]
        lines += ["  - action: edit", f'    resource: "{secret}"', "    effect: deny"]
    lines.append("---")
    lines.append("")
    body = [
        "Eres un agente para tareas sencillas que trabaja en el equipo del usuario con un modelo local.",
        "Nada de lo que hagas sale de aqui.",
        "",
        "## Como trabajas",
        "",
        "- No hagas preguntas: ejecuta la tarea con lo que tengas y anota al final cualquier suposicion.",
        "- Lee lo justo. Si te piden un resumen de 5 ficheros, no los pegues en tu respuesta.",
        "- Prefiere `grep` y `glob` antes que `read` sobre ficheros enteros.",
        "- Haz los cambios grandes en pasos pequenos y comprueba que el proyecto sigue en pie.",
    ]
    if profile.test_command:
        body.append(f"- Antes de dar por terminado, ejecuta `{profile.test_command}` y reporta el resultado.")
    if profile.lint_command:
        body.append(f"- Si tocas formato o estilo, comprueba con `{profile.lint_command}`.")
    body += [
        "",
        "## Que devuelves",
        "",
        "Un resumen de maximo 15 lineas:",
        "",
        "- Que has cambiado o encontrado, agrupado por fichero.",
        "- Rutas y lineas concretas, no contenidos.",
        "- Comandos que has ejecutado y su resultado.",
        "- Dudas o suposiciones que necesite que el padre decida.",
        "",
        "Nunca devuelvas el contenido completo de un fichero. Debes consumir tu propio contexto,",
        "no el del padre.",
    ]
    return "\n".join(lines + body) + "\n"


# --------------------------------------------------------------------------- #
# Barato
# --------------------------------------------------------------------------- #


def cheap_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    description = (
        "Tareas mecanicas y poco criticas con un modelo pequeno y barato. Renombrados, formateo, "
        "redaccion simple, buscar referencias, primera pasada de refactors."
    )
    lines = _frontmatter(agent, description)
    lines += _deny_all(["subagent", "webfetch"])
    lines += ["  - action: shell", '    resource: "*"', "    effect: ask"]
    lines += ["  - action: edit", '    resource: "*"', "    effect: ask"]
    lines.append("---")
    lines.append("")
    body = [
        "Eres un agente definido para realizar tareas mecanicas o sencillas.",
        "",
        "## Que haces",
        "",
        "- Renombrar, mover y formatear siguiendo el estilo del proyecto.",
        "- Corregir typos y redaccion menor en comentarios o documentacion.",
        "- Localizar referencias y devolver rutas.",
        "- Preparar el terreno: buscar, listar y proponer un primer cambio.",
    ]
    if profile_has_tests(plan.profile):
        body.append(f"- Ejecutar `{plan.profile.test_command}` y resumir fallos por ruta.")
    body += [
        "",
        "## Que no haces",
        "",
        "- No tomas decisiones de diseño.",
        "- No refactorizas la arquitectura.",
        "- Si la tarea es ambigua, eliges la interpretacion mas conservadora, la haces y avisas de la",
        "  suposicion en el resumen.",
        "",
        "## Como respondes",
        "",
        "Maximo 10 lineas. Dices que cambiaste y donde, nada mas. No explicas el porqué.",
    ]
    return "\n".join(lines + body) + "\n"


def profile_has_tests(profile: ProjectProfile) -> bool:
    return bool(profile.test_command)


# --------------------------------------------------------------------------- #
# Especialista
# --------------------------------------------------------------------------- #


def specialist_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    profile = plan.profile
    description = (
        f"Trabajo profundo de dominio sobre {profile.stack_label}: cambios transversales, "
        "diseno de solucion, refactors grandes y debugging. Puede editar ficheros."
    )
    lines = _frontmatter(agent, description)
    lines += _deny_all(["subagent", "webfetch", "websearch"])
    # El especialista es el unico agente caro con permiso de editar, asi que puede
    # (y debe) sacar el trabajo mecanico a los agentes baratos en vez de hacerlo con
    # su propio contexto.
    mechanical = [a.name for a in plan.subagents if a.tier in ("local", "cheap")]
    for target in mechanical:
        lines += _allow("subagent", target)
    for secret in profile.secret_paths[:20]:
        lines += ["  - action: read", f'    resource: "{secret}"', "    effect: deny"]
    lines.append("---")
    lines.append("")
    delegate_hint = (
        "Delega el trabajo mecanico (lecturas masivas, refactors, formateo) en "
        f"`{'` o `'.join(mechanical)}` en lugar de hacerlo tu: tu contexto es el caro."
        if mechanical
        else "No tienes subagentes disponibles: hazlo tu pero sin extenderte."
    )
    body = [
        f"Eres el agente especialista de `{profile.name}`. Recibes problemas ya delimitados por el",
        "orquestador, no peticiones abiertas.",
        "",
        "## Dominio",
        "",
    ]
    if "laravel" in profile.frameworks:
        body += [
            "- Laravel: respeta las convenciones del proyecto (Eloquent, Form Requests, Policies, Jobs/Events).",
            "- No introduzgas paquetes nuevos sin avisar: primero busca si ya existe una solucion interna.",
            "- Cuidado con N+1 y con consultas dentro de bucles.",
        ]
    if "python" in profile.frameworks:
        body += [
            "- Python: tipado cuando exista en el proyecto, docstrings en la API publica.",
            "- No anadas dependencias sin avisar; usa primero la biblioteca estandar.",
        ]
    if "node" in profile.frameworks:
        body += [
            "- Node/TS: respeta el sistema de modulos y el estilo del proyecto.",
            "- No anadas dependencias sin avisar; comprueba primero el lockfile.",
        ]
    if profile.build_command:
        body.append(f"- El proyecto se prepara con `{profile.build_command}`.")
    if profile.test_command:
        body.append(f"- Los tests se ejecutan con `{profile.test_command}`: es parte del trabajo, no un extra.")
    if profile.lint_command:
        body.append(f"- El estilo se comprueba con `{profile.lint_command}`.")
    if profile.secret_paths:
        body.append(f"- Jamás leas ni edites: {', '.join(profile.secret_paths[:5])}.")
    body += [
        "",
        "## Como trabajas",
        "",
        delegate_hint,
        "- Cambios pequenos y verificables; no mezcles refactor y funcionalidad en el mismo paso.",
        "- Si el problema no es el que creias, paralo y devuelve el hallazgo con evidencia.",
        "",
        "## Que devuelves",
        "",
        "Resumen de maximo 20 lineas: decision tomada, ficheros y lineas tocadas, comandos",
        "ejecutados con su resultado, y riesgos que queden abiertos.",
    ]
    return "\n".join(lines + body) + "\n"


RENDERERS = {
    "orquestador": lambda plan, agent: orchestrator_agent(plan),
    "local": lambda plan, agent: local_agent(plan, agent),
    "barato": lambda plan, agent: cheap_agent(plan, agent),
    "especialista": lambda plan, agent: specialist_agent(plan, agent),
}


def render_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    return RENDERERS[agent.name](plan, agent)


# --------------------------------------------------------------------------- #
# Config opencode.jsonc
# --------------------------------------------------------------------------- #


def default_agent_snippet(agent_name: str) -> str:
    return f'{{\n  "$schema": "https://opencode.ai/config.json",\n  "default_agent": "{agent_name}"\n}}\n'


def provider_snippet(patch: dict) -> str:
    return (
        '"' + patch["provider"] + '": {\n'
        '  "npm": "@ai-sdk/openai-compatible",\n'
        f'  "name": "{patch["provider"]}",\n'
        '  "options": {\n'
        f'    "baseURL": "{patch["baseURL"]}",\n'
        '    "apiKey": "local"\n'
        "  },\n"
        '  "models": {\n'
        f'    "{patch["model_id"]}": {{ "name": "{patch["model_id"]}" }}\n'
        "  }\n"
        "}"
    )