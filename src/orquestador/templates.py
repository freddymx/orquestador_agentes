"""Plantillas de agentes OpenCode V2 y del bloque de configuracion.

Todo se genera a partir del perfil del proyecto y del plan, sin texto muerto:
si el proyecto no tiene tests, no se menciona el comando de test.
"""

from __future__ import annotations

import json

from .catalog import TIER_HINTS  # noqa: F401  (reexportado por comodidad)
from .planner import AgentPlan, OrchestrationPlan, yaml_scalar
from .scan import ProjectProfile


def _one_line(text: str, limit: int = 300) -> str:
    """Descripcion segura para el frontmatter: una linea, sin comillas dobles."""
    cleaned = " ".join(str(text).split()).replace('"', "'")
    return cleaned[:limit]


def _frontmatter(plan: AgentPlan, description: str) -> list[str]:
    rendered = json.dumps(_one_line(description), ensure_ascii=False)
    lines = ["---", f"description: {rendered}", f"mode: {plan.role}"]
    if plan.model:
        lines.append(f"model: {yaml_scalar(plan.model)}")
    if plan.steps:
        lines.append(f"steps: {plan.steps}")
    lines.append(f'color: "{plan.color}"')
    return lines


def _deny_all(actions: list[str]) -> list[str]:
    lines = ["permissions:"]
    for action in actions:
        lines += [f"  - action: {action}", '    resource: "*"', "    effect: deny"]
    return lines


def _deny(action: str, resource: str) -> list[str]:
    return [
        f"  - action: {action}",
        f"    resource: {json.dumps(str(resource), ensure_ascii=False)}",
        "    effect: deny",
    ]


def _allow(action: str, resource: str) -> list[str]:
    rendered = '    resource: "*"' if resource == "*" else f"    resource: {yaml_scalar(resource)}"
    return [f"  - action: {action}", rendered, "    effect: allow"]


def _ask(action: str, resource: str = "*") -> list[str]:
    return [
        f"  - action: {action}",
        f"    resource: {json.dumps(str(resource), ensure_ascii=False)}",
        "    effect: ask",
    ]


def _strict(plan: OrchestrationPlan) -> bool:
    return plan.strategy.preset == "strict"


SECRET_ACTIONS = ("read", "edit")

# Guardarrailes, no un sandbox: el shell se compara como texto y se puede sortear
# con alias o rutas absolutas. Sirven para que el agente local no saque datos por
# accidente, no para parar a alguien que quiera hacerlo.
NETWORK_SHELL_PATTERNS = ("curl *", "wget *", "nc *", "ssh *", "scp *", "git push *")


def _secret_rules(profile: ProjectProfile, actions=SECRET_ACTIONS) -> list[str]:
    lines: list[str] = []
    for secret in profile.secret_paths:
        for action in actions:
            lines += _deny(action, secret)
    return lines


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
    local = next((a for a in plan.subagents if a.name == "local"), None)
    cheap = next((a for a in plan.subagents if a.name == "barato"), None)

    routing: list[str] = []
    for agent in plan.subagents:
        if agent.name == "local":
            hint = "nano y small. Leer, buscar, editar en masa, refactors mecanicos."
        elif agent.name == "barato":
            hint = "medium. Renombrados, formateo, redaccion simple, referencias."
        elif agent.name == "especialista":
            hint = (
                "large. Diseno, cambios transversales, trabajo profundo de dominio. "
                "Delegable aunque modifique codigo."
            )
        else:
            hint = _one_line(agent.reason or "trabajo especializado")
        model_hint = " ".join(str(agent.model or "—").split()).replace("`", "'")
        routing.append(f"- `{agent.name}` ({model_hint}): {hint}")
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
        if _strict(plan):
            context.append(
                f"- **Tests**: `{profile.test_command}`. En strict no hay shell: pide al usuario "
                "que los ejecute antes de dar el cambio por bueno."
            )
        else:
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
    if _strict(plan):
        rules.append(
            "- Modo strict: los subagentes no tienen shell; si hace falta ejecutar algo, "
            "pidelo al usuario o limita el cambio a edicion de ficheros."
        )
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
    denied = ["external_directory", "edit", "subagent", "shell"]
    if _strict(plan):
        denied += ["webfetch", "websearch"]
    lines += _deny_all(denied)
    lines += _secret_rules(plan.profile, ("read",))
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
    lines += _deny_all(["external_directory", "subagent", "webfetch", "websearch"])
    if _strict(plan):
        lines += _deny("shell", "*")
    for pattern in NETWORK_SHELL_PATTERNS:
        lines += _deny("shell", pattern)
    lines += _secret_rules(profile)
    lines.append("---")
    lines.append("")
    body = [
        "Eres un agente para tareas sencillas que trabaja en el equipo del usuario con un modelo local.",
        "Tu modelo corre en esta maquina: lo que lees no se envia a ningun proveedor.",
        "Los comandos de red (curl, wget, ssh, scp, git push) estan bloqueados: si necesitas red,",
        "para y dilo en el resumen en vez de buscar un rodeo.",
        "",
        "## Como trabajas",
        "",
        "- No hagas preguntas: ejecuta la tarea con lo que tengas y anota al final cualquier suposicion.",
        "- Lee lo justo. Si te piden un resumen de 5 ficheros, no los pegues en tu respuesta.",
        "- Prefiere `grep` y `glob` antes que `read` sobre ficheros enteros.",
        "- Haz los cambios grandes en pasos pequenos y comprueba que el proyecto sigue en pie.",
    ]
    if _strict(plan):
        body.append(
            "- No tienes shell: no puedes ejecutar tests; termina el cambio y deja el comando "
            "que deberia ejecutarse en el resumen."
        )
    else:
        if profile.test_command:
            body.append(
                f"- Antes de dar por terminado, ejecuta `{profile.test_command}` y reporta el resultado."
            )
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
        (
            "- Comandos que deberian ejecutarse (no tienes shell)."
            if _strict(plan)
            else "- Comandos que has ejecutado y su resultado."
        ),
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
    if _strict(plan):
        lines += _deny_all(["external_directory", "subagent", "webfetch", "websearch", "shell"])
        lines += _ask("edit")
    else:
        lines += _deny_all(["external_directory", "subagent", "webfetch"])
        lines += _ask("shell")
        lines += _ask("edit")
    lines += _secret_rules(plan.profile)
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
    if _strict(plan):
        body.append("- No tienes shell: no ejecutes tests; deja el comando pendiente en el resumen.")
    elif profile_has_tests(plan.profile):
        body.append(f"- Ejecutar `{plan.profile.test_command}` y resumir fallos por ruta.")
    body += [
        "",
        "## Que no haces",
        "",
        "- No tomas decisiones de diseño.",
        "- No refactorizas la arquitectura.",
        "- No lees ni editas ficheros de secretos (.env, claves): estan denegados y tu modelo no es local.",
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
    denied = ["external_directory", "subagent", "webfetch", "websearch"]
    if _strict(plan):
        denied.append("shell")
    lines += _deny_all(denied)
    if _strict(plan):
        lines += _ask("edit")
    # El especialista es el unico agente caro con permiso de editar, asi que puede
    # (y debe) sacar el trabajo mecanico a los agentes baratos en vez de hacerlo con
    # su propio contexto.
    mechanical = [a.name for a in plan.subagents if a.tier in ("local", "cheap")]
    for target in mechanical:
        lines += _allow("subagent", target)
    lines += _secret_rules(profile)
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
    if "python" in profile.runtimes:
        body += [
            "- Python: tipado cuando exista en el proyecto, docstrings en la API publica.",
            "- No anadas dependencias sin avisar; usa primero la biblioteca estandar.",
        ]
    if "node" in profile.runtimes:
        body += [
            "- Node/TS: respeta el sistema de modulos y el estilo del proyecto.",
            "- No anadas dependencias sin avisar; comprueba primero el lockfile.",
        ]
    if profile.build_command:
        body.append(f"- El proyecto se prepara con `{profile.build_command}`.")
    if _strict(plan):
        body.append(
            "- No tienes shell: no ejecutes tests ni lint; deja los comandos pendientes en el resumen."
        )
    else:
        if profile.test_command:
            body.append(
                f"- Los tests se ejecutan con `{profile.test_command}`: es parte del trabajo, no un extra."
            )
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
        (
            "pendientes de ejecutar (no tienes shell), y riesgos que queden abiertos."
            if _strict(plan)
            else "ejecutados con su resultado, y riesgos que queden abiertos."
        ),
    ]
    return "\n".join(lines + body) + "\n"


def custom_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    """Render de un subagente definido por el usuario (o fallback generico)."""
    profile = plan.profile
    custom = agent.custom
    name = agent.name
    description = (
        custom.description
        if custom is not None and custom.description
        else f"Subagente personalizado '{name}' para tareas concretas del proyecto."
    )
    permissions = custom.permissions if custom is not None else "edit"
    prompt = custom.prompt if custom is not None else ""
    strict = _strict(plan)

    lines = _frontmatter(agent, description)
    denied = ["external_directory", "subagent", "webfetch", "websearch"]
    if strict or permissions == "read":
        denied.append("shell")
    lines += _deny_all(denied)
    if permissions == "read":
        lines += _deny("edit", "*")
    elif strict:
        lines += _ask("edit")
    else:
        lines += _allow("edit", "*")
        lines += _allow("shell", "*") if permissions == "full" else _ask("shell")
    lines += _secret_rules(profile)
    lines.append("---")
    lines.append("")

    instructions = prompt or (
        "Trabajas dentro del alcance que te da el orquestador. Si el encargo no esta claro, "
        "elige la interpretacion mas conservadora, hazla y deja la suposicion en el resumen."
    )
    body = [
        f"Eres el subagente `{name}` del proyecto `{profile.name}`.",
        "",
        "## Encargo",
        "",
        description,
        "",
        "## Instrucciones",
        "",
        instructions,
        "",
        "## Contexto del proyecto",
        "",
        f"- **Stack**: {profile.stack_label}.",
    ]
    if profile.key_dirs:
        body.append("- **Carpetas clave**: " + ", ".join(f"`{d}/`" for d in profile.key_dirs[:8]) + ".")
    if profile.test_command:
        body.append(f"- **Tests**: `{profile.test_command}`.")
    if profile.lint_command:
        body.append(f"- **Lint/estilo**: `{profile.lint_command}`.")
    if profile.secret_paths:
        body.append(
            "- **No leer ni editar**: " + ", ".join(f"`{p}`" for p in profile.secret_paths[:5]) + "."
        )
    body += ["", "## Limites", ""]
    if permissions == "read":
        body.append("- Solo lectura: no edites ficheros ni ejecutes comandos de shell; devuelve hallazgos.")
    elif strict:
        body.append("- Modo strict: cada edicion requiere confirmacion y no tienes shell.")
    elif permissions == "full":
        body.append("- Puedes editar y ejecutar shell, pero no salgas del proyecto ni toques secretos.")
    else:
        body.append("- Puedes editar; el shell requiere confirmacion. No toques secretos.")
    body += [
        "- No delegues en otros agentes: tu trabajo es resolver el encargo.",
        (
            "- Devuelve un resumen corto: que has hecho, rutas y lineas, comandos pendientes "
            "(no tienes shell) y dudas."
            if strict or permissions == "read"
            else "- Devuelve un resumen corto: que has hecho, rutas y lineas, comandos y resultados, dudas."
        ),
        "- Nunca devuelvas el contenido completo de un fichero.",
    ]
    return "\n".join(lines + body) + "\n"


RENDERERS = {
    "orquestador": lambda plan, agent: orchestrator_agent(plan),
    "local": lambda plan, agent: local_agent(plan, agent),
    "barato": lambda plan, agent: cheap_agent(plan, agent),
    "especialista": lambda plan, agent: specialist_agent(plan, agent),
}


def render_agent(plan: OrchestrationPlan, agent: AgentPlan) -> str:
    if agent.custom is not None or agent.name not in RENDERERS:
        return custom_agent(plan, agent)
    return RENDERERS[agent.name](plan, agent)


# --------------------------------------------------------------------------- #
# Config opencode.jsonc
# --------------------------------------------------------------------------- #


def default_agent_snippet(agent_name: str) -> str:
    return f'{{\n  "$schema": "https://opencode.ai/config.json",\n  "default_agent": "{agent_name}"\n}}\n'


def provider_snippet(patch: dict) -> str:
    provider = json.dumps(str(patch["provider"]), ensure_ascii=False)
    base_url = json.dumps(str(patch["baseURL"]), ensure_ascii=False)
    model_id = json.dumps(str(patch["model_id"]), ensure_ascii=False)
    return (
        f"{provider}: {{\n"
        '  "npm": "@ai-sdk/openai-compatible",\n'
        f'  "name": {provider},\n'
        '  "options": {\n'
        f'    "baseURL": {base_url},\n'
        '    "apiKey": "local"\n'
        "  },\n"
        '  "models": {\n'
        f'    {model_id}: {{ "name": {model_id} }}\n'
        "  }\n"
        "}"
    )