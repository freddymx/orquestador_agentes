"""CLI de `orq`: detecta, planifica y genera el orquestador de agentes."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .catalog import blended_price, build_catalog, merge_configs
from .cost import DEFAULT_MIX
from .cost import estimate as estimate_plan
from .planner import (
    Strategy,
    StrategyError,
    auto_strategy,
    build_plan,
    load_strategy,
    normalize_preset,
    tier_of,
)
from .scan import scan_project
from .writer import (
    STRATEGY_FILE,
    agents_dir,
    parse_agent_file,
    permission_rules,
    planned_files,
    validate,
    write_plan,
)


def _version_callback(value: bool) -> None:
    from . import __version__

    if value:
        console.print(f"orq {__version__}")
        raise typer.Exit(0)


app = typer.Typer(
    add_completion=False,
    help="Genera un orquestador de agentes para OpenCode V2 adaptado a tu proyecto y tus modelos.",
)
console = Console()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Muestra la version y sale.",
    ),
) -> None:
    """orq: genera el orquestador de agentes de un proyecto."""

TIER_STYLE = {"frontier": "bold magenta", "cheap": "green", "local": "orange", "desconocido": "dim"}


def _resolve_dir(directory: str | None) -> Path:
    return Path(directory).resolve() if directory else Path.cwd()


SCOPES = ("project", "global")


def _fail(message: str) -> NoReturn:
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(2)


def _resolve_scope(scope: str | None, strategy: Strategy | None = None) -> str:
    """Prioridad: --scope explicito > scope de la estrategia > project."""
    value = (scope or (strategy.scope if strategy else None) or "project").strip().lower()
    if value not in SCOPES:
        _fail(f"scope debe ser 'project' o 'global' (recibido: '{value}')")
    return value


def _strategy_path(project_dir: Path, strategy_file: Path | None) -> Path:
    return Path(strategy_file) if strategy_file else project_dir / STRATEGY_FILE


def _load_strategy_file(path: Path, required: bool = False) -> Strategy:
    if required and not path.exists():
        _fail(f"No existe la estrategia: {path}")
    try:
        return load_strategy(path if path.exists() else None)
    except StrategyError as exc:
        _fail(f"Estrategia invalida en {path}: {exc}")


def _load_context(project_dir: Path, probe: bool, endpoint: list[str] | None, timeout: float):
    config, sources = merge_configs(project_dir)
    catalog = build_catalog(config, sources, project_dir, probe=probe, timeout=timeout, extra_endpoints=endpoint)
    profile = scan_project(project_dir)
    return profile, catalog, sources


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #


@app.command()
def detect(
    directory: str = typer.Option(".", "--dir", "-d", help="Proyecto a analizar"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Sondear endpoints locales"),
    endpoint: list[str] = typer.Option(None, "--endpoint", "-e", help="Endpoint OpenAI-compatible extra"),
    as_json: bool = typer.Option(False, "--json", help="Salida JSON para scripts"),
):
    """Muestra el stack del proyecto y los modelos disponibles."""
    project_dir = _resolve_dir(directory)
    profile, catalog, sources = _load_context(project_dir, probe, endpoint, 2.0)

    if as_json:
        console.print_json(data={
            "project": {
                "name": profile.name,
                "root": str(profile.root),
                "stack": profile.stack_label,
                "primary_language": profile.primary_language,
                "languages": profile.languages,
                "runtimes": profile.runtimes,
                "frameworks": profile.frameworks,
                "key_dirs": profile.key_dirs,
                "test_command": profile.test_command,
                "lint_command": profile.lint_command,
                "build_command": profile.build_command,
                "secret_paths": profile.secret_paths,
                "file_count": profile.file_count,
                "is_monorepo": profile.is_monorepo,
                "signals": [
                    {"kind": s.kind, "path": s.path, "detail": s.detail} for s in profile.signals
                ],
            },
            "config_sources": [str(s) for s in sources],
            "providers": {
                name: {
                    "base_url": p.base_url,
                    "is_local": p.is_local,
                    "declared_models": sorted(p.declared_models),
                }
                for name, p in catalog.providers.items()
            },
            "models": [
                {
                    "ref": m.ref,
                    "tier": tier_of(catalog, m.ref),
                    "source": m.source,
                    "tools": m.supports_tools,
                    "price_per_million": round(blended_price(m, catalog.providers.get(m.provider)), 4),
                }
                for m in sorted(catalog.models, key=lambda m: (tier_of(catalog, m.ref), m.ref))
            ],
            "excluded_models": sorted(m.model_id for m in catalog.excluded),
            "probed": {base: len(found) for base, found in catalog.probed.items()},
        })
        raise typer.Exit(0)

    body = [f"[bold]Proyecto[/bold]: {profile.name}", f"[bold]Stack[/bold]: {profile.stack_label}"]
    if profile.languages:
        body.append("[bold]Lenguajes[/bold]: " + ", ".join(f"{k} ({v})" for k, v in list(profile.languages.items())[:6]))
    if profile.key_dirs:
        body.append("[bold]Carpetas clave[/bold]: " + ", ".join(profile.key_dirs))
    if profile.test_command:
        body.append(f"[bold]Tests[/bold]: {profile.test_command}")
    if profile.lint_command:
        body.append(f"[bold]Lint[/bold]: {profile.lint_command}")
    if profile.secret_paths:
        body.append(f"[bold]Protegidos[/bold]: {', '.join(profile.secret_paths[:6])}")
    body.append(f"[bold]Ficheros de codigo[/bold]: {profile.file_count}")
    if sources:
        body.append("[dim]Config leida: " + ", ".join(str(s) for s in sources) + "[/dim]")
    console.print(Panel("\n".join(body), title="Proyecto", border_style="cyan"))

    if profile.signals:
        table = Table(title="Senarales", show_header=True, header_style="dim")
        table.add_column("tipo")
        table.add_column("detalle")
        for signal in profile.signals:
            table.add_row(signal.kind, signal.detail)
        console.print(table)

    _print_models(catalog)


def _print_models(catalog) -> None:
    if not catalog.models:
        console.print("[yellow]No se ha detectado ningun modelo de chat.[/yellow]")
        console.print(
            "Define uno en opencode.jsonc (provider + models) o pasa --endpoint http://host:port/v1"
        )
    else:
        table = Table(title="Modelos de chat detectados", header_style="dim")
        table.add_column("modelo")
        table.add_column("tier")
        table.add_column("origen")
        table.add_column("tools")
        for model in sorted(catalog.models, key=lambda m: (tier_of(catalog, m.ref), m.ref)):
            tier = tier_of(catalog, model.ref)
            table.add_row(
                model.ref,
                f"[{TIER_STYLE.get(tier, '')}]{tier}[/]",
                model.source,
                "si" if model.supports_tools else "no",
            )
        console.print(table)

    if catalog.excluded:
        names = ", ".join(sorted(m.model_id for m in catalog.excluded)[:8])
        extra = " ..." if len(catalog.excluded) > 8 else ""
        console.print(
            f"[dim]descartados (embeddings/TTS/ASR, no sirven como agente): {names}{extra}[/dim]"
        )

    for base, found in catalog.probed.items():
        status = f"{len(found)} modelos" if found else "sin respuesta"
        console.print(f"[dim]sondeo {base}: {status}[/dim]")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


@app.command()
def models(
    directory: str = typer.Option(".", "--dir", "-d"),
    endpoint: list[str] = typer.Option(None, "--endpoint", "-e"),
    timeout: float = typer.Option(2.0, "--timeout", help="Timeout de sondeo en segundos"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Sondear endpoints locales"),
):
    """Lista los modelos disponibles y el tier asignado a cada uno."""
    project_dir = _resolve_dir(directory)
    _, catalog, _ = _load_context(project_dir, probe, endpoint, timeout)
    _print_models(catalog)

    strategy, notes = auto_strategy(catalog, scan_project(project_dir))
    console.print(Panel(
        "\n".join([
            f"orquestador : {strategy.orchestrator_model or '—'}",
            f"especialista: {strategy.specialist_model or '—'}",
            f"barato      : {strategy.cheap_model or '—'}",
            f"local       : {strategy.local_model or '—'}",
        ]),
        title="Seleccion automatica",
        border_style="magenta",
    ))
    for note in notes:
        console.print(f"  · {note}")


# --------------------------------------------------------------------------- #
# init (interactivo)
# --------------------------------------------------------------------------- #


@app.command()
def init(
    directory: str = typer.Option(".", "--dir", "-d", help="Proyecto a orquestar"),
    scope: str = typer.Option(None, "--scope", help="project | global (por defecto: project)"),
    preset: str = typer.Option(None, "--preset", help="balanced | lean | strict"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Sondear endpoints locales"),
    force: bool = typer.Option(False, "--force", help="Sobrescribir agentes existentes"),
    backup: bool = typer.Option(False, "--backup", help="Guardar .bak antes de sobrescribir"),
):
    """Genera el orquestador de forma guiada."""
    project_dir = _resolve_dir(directory)
    profile, catalog, _ = _load_context(project_dir, probe, None, 2.0)
    strategy, notes = auto_strategy(catalog, profile)
    if preset:
        try:
            strategy.preset = normalize_preset(preset)
        except StrategyError as exc:
            _fail(str(exc))
    resolved_scope = _resolve_scope(scope, strategy)

    console.print(Panel(
        f"{profile.name} · {profile.stack_label}\n"
        f"ficheros de codigo: {profile.file_count}"
        + (f"\ntests: {profile.test_command}" if profile.test_command else ""),
        title="Proyecto detectado",
        border_style="cyan",
    ))

    _print_models(catalog)
    console.print("\n[bold]Seleccion propuesta[/bold]")
    for note in notes:
        console.print(f"  · {note}")
    if preset is None:
        strategy.preset = _ask_preset(strategy.preset)
    else:
        console.print(f"  · Preset: {strategy.preset}")

    if catalog.models:
        _ask_models(strategy, catalog)

    extra = typer.prompt("Reglas extra para el orquestador (una por linea, vacio para terminar)", default="", show_default=False)
    if extra:
        strategy.extra_rules = [line.strip() for line in extra.splitlines() if line.strip()]

    strategy.default_agent = typer.confirm(
        "Poner el orquestador como agente por defecto (default_agent)?", default=True
    )
    if strategy.preset == "lean":
        console.print(
            "[dim]Preset lean: un solo trabajador (local > barato > especialista). "
            "Los roles personalizados no se generan.[/dim]"
        )
    else:
        strategy.add_specialist = typer.confirm(
            "Generar tambien el agente 'especialista' (frontera, puede editar)?", default=True
        )
    console.print(
        "[dim]Roles extra: edita .orquestador.yaml y anade subagents.<nombre> con "
        "model, steps, description, prompt y permissions.[/dim]"
    )

    plan = build_plan(profile, catalog, strategy)
    _preview(plan)
    if not typer.confirm("Escribir los ficheros?", default=True):
        console.print("[yellow]Cancelado. No se ha escrito nada.[/yellow]")
        raise typer.Exit(0)

    result = write_plan(plan, project_dir, scope=resolved_scope, force=force, backup=backup)
    _report(result)
    raise typer.Exit(0)


def _ask_preset(current: str = "balanced") -> str:
    console.print(
        "[bold]Preset de jerarquia[/bold]\n"
        "  balanced: orquestador + local/barato/especialista (recomendado).\n"
        "  lean    : orquestador + un solo trabajador (local > barato > especialista).\n"
        "  strict  : como balanced, pero sin shell en subagentes y ediciones cloud con confirmacion."
    )
    while True:
        raw = typer.prompt("Preset (balanced/lean/strict)", default=current)
        try:
            return normalize_preset(raw)
        except StrategyError as exc:
            console.print(f"[red]{exc}[/red]")


def _ask_models(strategy: Strategy, catalog) -> None:
    """Pregunta por cada rol. Si un tier esta vacio se ofrecen TODOS los modelos
    (marcando su tier real): puede que solo tengas locales y aun asi quieras
    decidir a mano como repartirlos."""
    if not catalog.models:
        return
    every = list(catalog.models)

    frontier = [m for m in every if tier_of(catalog, m.ref) == "frontier"]
    cheap = [m for m in every if tier_of(catalog, m.ref) == "cheap"]
    local = [m for m in every if tier_of(catalog, m.ref) == "local"]

    strategy.orchestrator_model = _ask_role(
        catalog, strategy.orchestrator_model, "orquestador", "frontera", frontier
    )
    strategy.specialist_model = strategy.orchestrator_model

    strategy.cheap_model = _ask_role(
        catalog, strategy.cheap_model, "barato", "barato", cheap,
        empty_hint="Sin este rol, el trabajo mecanico cae en 'local' o 'especialista'.",
    )
    strategy.local_model = _ask_role(
        catalog, strategy.local_model, "local", "local", local,
        empty_hint="Sin este rol no se genera el agente 'local'.",
    )


TIER_TITLES = {
    "frontera": "Modelos frontera (decidir, disenar, enfocar)",
    "barato": "Modelos baratos (mecanicos en la nube)",
    "local": "Modelos locales (privados, coste cero)",
}


def _ask_role(catalog, current: str | None, label: str, tier: str,
              options: list, empty_hint: str = "") -> str | None:
    if options:
        console.print(f"\n[bold]{TIER_TITLES.get(tier, tier)}[/bold]")
    else:
        console.print(
            f"\n[yellow]No hay modelos de tier '{tier}'.[/yellow] Elige igualmente "
            "uno de los disponibles."
        )
        if label == "orquestador":
            console.print(
                "[dim]El orquestador necesita un modelo fijo: sin el heredaria el de la "
                "sesion padre, que es justo lo caro que se quiere evitar.[/dim]"
            )
        else:
            console.print(
                "[dim]Puedes dejar este rol vacio (0) y el trabajo caera en otro agente.[/dim]"
            )
        options = list(catalog.models)
        if not options:
            console.print("[red]No hay ningun modelo disponible.[/red]")
            return None

    show_tier = not any(tier_of(catalog, m.ref) == tier for m in options)
    for i, model in enumerate(options, 1):
        suffix = f"  [dim]({tier_of(catalog, model.ref)})[/dim]" if show_tier else ""
        console.print(f"  {i}. {model.ref}{suffix}")

    chosen = _choose(options, current, label)
    if chosen is None and empty_hint:
        console.print(f"  [dim]'{label}' se queda sin modelo fijo. {empty_hint}[/dim]")
    return chosen


def _choose(options, current: str | None, label: str) -> str | None:
    if not options:
        return None
    refs = [m.ref for m in options]
    default_index = refs.index(current) + 1 if current in refs else 1
    while True:
        raw = typer.prompt(f"Modelo para '{label}' (1-{len(options)}, 0 para ninguno)", default=default_index)
        try:
            index = int(raw)
        except ValueError:
            console.print("[red]Introduce un numero.[/red]")
            continue
        if index == 0:
            return None
        if 1 <= index <= len(options):
            return refs[index - 1]
        console.print("[red]Fuera de rango.[/red]")


# --------------------------------------------------------------------------- #
# generate (no interactivo)
# --------------------------------------------------------------------------- #


@app.command()
def generate(
    directory: str = typer.Option(".", "--dir", "-d"),
    strategy_file: Path = typer.Option(None, "--strategy", "-s", help=f"Estrategia YAML (por defecto {STRATEGY_FILE})"),
    scope: str = typer.Option(None, "--scope", help="project | global (por defecto: el de la estrategia)"),
    orchestrator_model: str = typer.Option(None, "--orchestrator-model"),
    cheap_model: str = typer.Option(None, "--cheap-model"),
    local_model: str = typer.Option(None, "--local-model"),
    preset: str = typer.Option(None, "--preset", help="balanced | lean | strict"),
    force: bool = typer.Option(False, "--force", help="Sobrescribir agentes existentes (nunca la config)"),
    backup: bool = typer.Option(False, "--backup", help="Guardar .bak antes de sobrescribir"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Sondear endpoints locales"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Muestra el contenido sin escribir"),
    as_json: bool = typer.Option(False, "--json", help="Salida JSON (con --dry-run incluye el contenido)"),
    no_default_agent: bool = typer.Option(False, "--no-default-agent"),
):
    """Genera los agentes de forma no interactiva usando la estrategia."""
    project_dir = _resolve_dir(directory)
    sf = _strategy_path(project_dir, strategy_file)
    strategy = _load_strategy_file(sf, required=strategy_file is not None)
    if not sf.exists():
        console.print(f"[dim]No hay estrategia en {sf}: se usara la seleccion automatica.[/dim]")
    resolved_scope = _resolve_scope(scope, strategy)
    if preset:
        try:
            strategy.preset = normalize_preset(preset)
        except StrategyError as exc:
            _fail(str(exc))

    for attr, value in (
        ("orchestrator_model", orchestrator_model),
        ("cheap_model", cheap_model),
        ("local_model", local_model),
    ):
        if value:
            setattr(strategy, attr, value)
    if no_default_agent:
        strategy.default_agent = False

    profile, catalog, _ = _load_context(project_dir, probe, None, 2.0)
    plan = build_plan(profile, catalog, strategy)

    if dry_run and as_json:
        from .templates import render_agent

        console.print_json(data={
            "project": plan.project,
            "preset": plan.strategy.preset,
            "notes": plan.notes,
            "provider_patches": plan.provider_patches,
            "agents": [
                {
                    "name": a.name,
                    "role": a.role,
                    "tier": a.tier,
                    "model": a.model,
                    "steps": a.steps,
                    "reason": a.reason,
                    "content": render_agent(plan, a),
                }
                for a in plan.agents
            ],
        })
        raise typer.Exit(0)

    _preview(plan)

    if dry_run:
        target_dir = agents_dir(resolved_scope, project_dir)
        for agent in plan.agents:
            console.print(Panel(
                (target_dir / f"{agent.name}.md").as_posix(),
                title=f"{agent.name}.md",
                border_style="dim",
            ))
            from .templates import render_agent

            console.print(render_agent(plan, agent))
        raise typer.Exit(0)

    result = write_plan(plan, project_dir, scope=resolved_scope, force=force, backup=backup)
    _report(result)

    problems = validate(
        project_dir,
        scope=resolved_scope,
        catalog=catalog,
        strategy=plan.strategy,
        profile=profile,
        planned_names={agent.name for agent in plan.agents},
        report_stale=False,
    )
    for level, message in problems:
        console.print(f"[{'red' if level == 'error' else 'yellow'}]{level}[/] {message}")
    raise typer.Exit(1 if any(level == "error" for level, _ in problems) else 0)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


@app.command()
def check(
    directory: str = typer.Option(".", "--dir", "-d"),
    scope: str = typer.Option(None, "--scope", help="project | global (por defecto: el de la estrategia)"),
    probe: bool = typer.Option(True, "--probe/--no-probe"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Valida los agentes generados contra el catalogo de modelos."""
    project_dir = _resolve_dir(directory)
    strategy = _load_strategy_file(project_dir / STRATEGY_FILE)
    resolved_scope = _resolve_scope(scope, strategy)
    config, sources = merge_configs(project_dir)
    catalog = build_catalog(config, sources, project_dir, probe=probe, timeout=1.5)
    profile = scan_project(project_dir)
    plan = build_plan(profile, catalog, strategy)
    planned_names = {agent.name for agent in plan.agents}

    problems = validate(
        project_dir,
        scope=resolved_scope,
        catalog=catalog,
        strategy=strategy,
        profile=profile,
        planned_names=planned_names,
    )
    if as_json:
        console.print_json(data={
            "ok": not any(level == "error" for level, _ in problems),
            "problems": [{"level": level, "message": message} for level, message in problems],
        })
        raise typer.Exit(1 if any(level == "error" for level, _ in problems) else 0)
    if not problems:
        console.print("[green]Todo correcto.[/green] orquestador + subagentes coherentes.")
        raise typer.Exit(0)
    for level, message in problems:
        colour = "red" if level == "error" else "yellow"
        console.print(f"[{colour}]{level.upper()}[/{colour}] {message}")
    raise typer.Exit(1 if any(level == "error" for level, _ in problems) else 0)


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


@app.command()
def diff(
    directory: str = typer.Option(".", "--dir", "-d"),
    strategy_file: Path = typer.Option(None, "--strategy", "-s"),
    scope: str = typer.Option(None, "--scope", help="project | global (por defecto: el de la estrategia)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Muestra que cambiaria `orq generate`, sin escribir nada.

    Sale con 0 si todo esta al dia y con 1 si hay diferencias (como `diff`).
    """
    project_dir = _resolve_dir(directory)
    sf = _strategy_path(project_dir, strategy_file)
    strategy = _load_strategy_file(sf, required=strategy_file is not None)
    resolved_scope = _resolve_scope(scope, strategy)
    profile, catalog, _ = _load_context(project_dir, False, None, 0.0)
    plan = build_plan(profile, catalog, strategy)
    files, notes = planned_files(plan, project_dir, resolved_scope)

    payload = []
    changed = 0
    for item in files:
        exists = item.path.exists()
        current = item.path.read_text(encoding="utf-8") if exists else ""
        if current == item.text:
            status = "unchanged"
        elif not exists:
            status = "added"
        else:
            status = "modified"
        if status != "unchanged":
            changed += 1
            if not as_json:
                typer.echo("".join(difflib.unified_diff(
                    current.splitlines(keepends=True),
                    item.text.splitlines(keepends=True),
                    fromfile=str(item.path) if exists else "/dev/null",
                    tofile=f"{item.path} (generado)",
                )), nl=False)
        payload.append({"path": str(item.path), "kind": item.kind, "status": status})

    if as_json:
        console.print_json(data={
            "ok": changed == 0,
            "differences": changed,
            "files": payload,
            "notes": notes,
        })
    else:
        for note in notes:
            console.print(f"[yellow]{note}[/yellow]")
        if changed == 0:
            console.print("[green]Sin diferencias: lo generado esta al dia.[/green]")
        else:
            console.print(f"[dim]{changed} fichero(s) cambiarian. Ejecuta orq generate para aplicarlo.[/dim]")
    raise typer.Exit(0 if changed == 0 else 1)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


@app.command()
def compare(
    directory: str = typer.Option(".", "--dir", "-d"),
    strategy_file: Path = typer.Option(None, "--strategy", "-s"),
    sessions: int = typer.Option(1, "--sessions", "-n", help="Sesiones a estimar"),
    tokens: int = typer.Option(None, "--tokens", help="Tokens por sesion (por defecto 100000)"),
    preset: str = typer.Option(None, "--preset", help="balanced | lean | strict"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Sondear endpoints locales"),
    local_share: float = typer.Option(None, "--local-share", help="%% del trabajo que va a local (0-1)"),
    cheap_share: float = typer.Option(None, "--cheap-share"),
    specialist_share: float = typer.Option(None, "--specialist-share"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Estima el coste del plan frente a hacerlo todo con el modelo del orquestador."""
    if sessions <= 0:
        _fail("--sessions debe ser mayor que 0")
    if tokens is not None and tokens <= 0:
        _fail("--tokens debe ser mayor que 0")
    project_dir = _resolve_dir(directory)
    sf = _strategy_path(project_dir, strategy_file)
    strategy = _load_strategy_file(sf, required=strategy_file is not None)
    if preset:
        try:
            strategy.preset = normalize_preset(preset)
        except StrategyError as exc:
            _fail(str(exc))
    profile, catalog, _ = _load_context(project_dir, probe, None, 2.0)
    plan = build_plan(profile, catalog, strategy)

    mix = dict(DEFAULT_MIX)
    for key, value in (
        ("local", local_share), ("cheap", cheap_share), ("specialist", specialist_share)
    ):
        if value is not None:
            mix[key] = value
    total = sum(mix.values())
    if total <= 0:
        console.print("[red]El reparto de trabajo no puede ser 0.[/red]")
        raise typer.Exit(1)
    mix = {k: v / total for k, v in mix.items()}

    estimate = estimate_plan(plan, catalog, sessions=sessions, mix=mix, token_budget=tokens)

    if as_json:
        console.print_json(data=estimate.as_dict())
        raise typer.Exit(0)

    table = Table(
        title=f"Reparto de trabajo ({sessions} sesion/es, {estimate.token_budget:,} tokens)",
        header_style="dim",
    )
    table.add_column("agente")
    table.add_column("modelo")
    table.add_column("% trabajo")
    table.add_column("$/1M")
    table.add_column("coste", justify="right")
    for agent in estimate.agents:
        price = "0 (local)" if agent.local else f"{agent.price_per_million:.2f}" + ("" if agent.exact else "*")
        table.add_row(
            agent.name,
            (agent.model or "—")[:46],
            f"{agent.share * 100:.0f}%",
            price,
            f"${agent.cost:.4f}",
        )
    console.print(table)

    if estimate.naive_cost > 0:
        headline = (
            f"Con orquestacion : ${estimate.total_cost:.4f}\n"
            f"Todo en el padre : ${estimate.naive_cost:.4f}\n"
            f"Ahorro estimado  : ${estimate.savings:.4f}  ({estimate.savings_ratio * 100:.0f}%)"
        )
        border = "magenta" if estimate.savings_ratio > 0.5 else "yellow"
    else:
        headline = (
            "Todos los modelos disponibles son locales: coste 0.\n"
            "Aqui lo que se gana no es dinero, es que nada sale de tu red."
        )
        border = "green"
    console.print(Panel(headline, title="Coste por sesion", border_style=border))

    if any(not a.exact and not a.local for a in estimate.agents):
        console.print("[dim]* precio no publicado en el catalogo: valor estimado[/dim]")
    for note in estimate.notes:
        console.print(f"[dim]· {note}[/dim]")
    console.print("[dim]Usa --local-share/--cheap-share o --tokens para ver escenarios distintos.[/dim]")
    raise typer.Exit(0)


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #


@app.command()
def agents(
    directory: str = typer.Option(".", "--dir", "-d"),
    scope: str = typer.Option(None, "--scope", help="project | global (por defecto: el de la estrategia)"),
    show: str = typer.Option(None, "--show", help="Imprime el cuerpo del agente indicado"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Aceptado por uniformidad; aqui no se sondea"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Lista los agentes generados y su reparto."""
    project_dir = _resolve_dir(directory)
    strategy = None
    strategy_path = project_dir / STRATEGY_FILE
    if strategy_path.exists():
        try:
            strategy = load_strategy(strategy_path)
        except StrategyError:
            strategy = None  # listar no debe fallar por una estrategia rota
    resolved_scope = _resolve_scope(scope, strategy)
    target = agents_dir(resolved_scope, project_dir)
    if not target.is_dir():
        console.print(f"[yellow]No hay agentes en {target}. Ejecuta primero: orq init[/yellow]")
        raise typer.Exit(1)

    entries = []
    for path in sorted(target.glob("*.md")):
        try:
            front, body = parse_agent_file(path)
        except ValueError as exc:
            console.print(f"[red]{path.name}: {exc}[/red]")
            continue
        entries.append((path, front, body))

    if show:
        match = [e for e in entries if e[0].stem == show]
        if not match:
            console.print(f"[yellow]No existe el agente '{show}'. Disponibles: "
                          f"{', '.join(e[0].stem for e in entries)}[/yellow]")
            raise typer.Exit(1)
        console.print(match[0][2].strip())
        raise typer.Exit(0)

    if as_json:
        console.print_json(data={
            "scope": resolved_scope,
            "agents": [
                {
                    "name": path.stem,
                    "description": front.get("description"),
                    "mode": front.get("mode"),
                    "model": front.get("model"),
                    "steps": front.get("steps"),
                    "can_subagent": sorted({
                        str(rule.get("resource")) for rule in permission_rules(front)
                        if rule.get("action") == "subagent" and rule.get("effect") == "allow"
                    }),
                }
                for path, front, _ in entries
            ],
        })
        raise typer.Exit(0)

    table = Table(title=f"Agentes ({resolved_scope})", header_style="dim")
    table.add_column("agente")
    table.add_column("modo")
    table.add_column("modelo")
    table.add_column("steps")
    table.add_column("puede llamar a")
    for path, front, _ in entries:
        table.add_row(
            path.stem,
            str(front.get("mode", "?")),
            str(front.get("model", "(hereda)")),
            str(front.get("steps", "—")),
            ", ".join(sorted({
                str(rule.get("resource")) for rule in permission_rules(front)
                if rule.get("action") == "subagent" and rule.get("effect") == "allow"
            })) or "—",
        )
    console.print(table)
    console.print("[dim]Cuerpo completo: orq agents --show <nombre>[/dim]")
    raise typer.Exit(0)


# --------------------------------------------------------------------------- #
# helpers de salida
# --------------------------------------------------------------------------- #


def _preview(plan) -> None:
    table = Table(title=f"Plan de agentes (preset: {plan.strategy.preset})", header_style="dim")
    table.add_column("agente")
    table.add_column("modo")
    table.add_column("modelo")
    table.add_column("por que")
    for agent in plan.agents:
        table.add_row(agent.name, agent.role, agent.model or "—", agent.reason)
    console.print(table)
    for note in plan.notes:
        console.print(f"  [dim]· {note}[/dim]")


def _report(result) -> None:
    if result.files:
        console.print("\n[green]Ficheros escritos:[/green]")
        for path in result.files:
            console.print(f"  ✓ {path}")
    if result.skipped:
        console.print("\n[yellow]No sobrescritos (usa --force):[/yellow]")
        for path in result.skipped:
            console.print(f"  · {path}")
    for note in result.notes:
        console.print(f"\n[yellow]Nota:[/yellow] {note}")
    console.print(
        "\n[bold]Siguiente paso[/bold]: abre opencode en el proyecto y usa el agente 'orquestador'.\n"
        "Comprueba el ahorro con: opencode stats"
    )


if __name__ == "__main__":
    app()