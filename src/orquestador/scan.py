"""Escaneo del proyecto: stack, comandos, carpetas sensibles y perfil de trabajo."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 4000
SKIP_DIRS = {
    ".git", "node_modules", "vendor", ".venv", "venv", "env", "__pycache__",
    "target", "dist", "build", ".next", ".nuxt", ".cache", ".idea", ".vscode",
    "storage", "coverage", ".svelte-kit", "tmp", ".pytest_cache", ".mypy_cache",
    "Pods", ".gradle", "bin", "obj", ".terraform",
}

SECRET_FILES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.pem|.*\.key|id_rsa.*|credentials.*|.*\.p12)$", re.IGNORECASE
)


@dataclass
class Signal:
    """Una senal detectada: ruta + por que importa."""

    kind: str
    path: str
    detail: str = ""


@dataclass
class ProjectProfile:
    root: Path
    name: str
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    test_command: str | None = None
    lint_command: str | None = None
    build_command: str | None = None
    secret_paths: list[str] = field(default_factory=list)
    key_dirs: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    file_count: int = 0
    is_monorepo: bool = False

    @property
    def primary_language(self) -> str:
        if not self.languages:
            return "desconocido"
        return max(self.languages.items(), key=lambda kv: kv[1])[0]

    @property
    def stack_label(self) -> str:
        if not self.frameworks:
            return self.primary_language
        return f"{self.primary_language}/{'+'.join(self.frameworks)}"

    def has_tests(self) -> bool:
        return bool(self.test_command)


EXTENSIONS = {
    ".php": "php", ".blade.php": "php",
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".rb": "ruby", ".cs": "csharp", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".vue": "vue", ".svelte": "svelte", ".ex": "elixir",
    ".exs": "elixir", ".sh": "shell",
}


def iter_files(root: Path):
    count = 0
    for path in root.rglob("*"):
        if count >= MAX_FILES:
            return
        # Ojo: comparar contra path.parts usaria la ruta ABSOLUTA, y ahi nombres
        # como "tmp", "bin" o "build" estan en SKIP_DIRS -> proyecto vacio.
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            count += 1
            yield path


def count_languages(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in iter_files(root):
        name = path.name.lower()
        lang = None
        if name.endswith(".blade.php"):
            lang = "php"
        else:
            lang = EXTENSIONS.get(path.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def detect_frameworks(root: Path) -> list[str]:
    found: list[str] = []

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    if has("composer.json", "artisan"):
        found.append("laravel")
        if (root / "artisan").exists():
            found.append("artisan")
    if has("package.json"):
        found.append("node")
    if has("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"):
        found.append("python")
    if has("Cargo.toml"):
        found.append("rust")
    if has("go.mod"):
        found.append("go")
    if has("docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
        found.append("docker")
    if has(".github/workflows"):
        found.append("ci")
    return found


def _read(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def detect_php_test_command(root: Path) -> str | None:
    if not (root / "composer.json").exists():
        return None
    if (root / "phpunit.xml").exists() or (root / "phpunit.xml.dist").exists():
        return "./vendor/bin/pest" if (root / "pest.php").exists() else "./vendor/bin/phpunit"
    if (root / "pest.php").exists():
        return "./vendor/bin/pest"
    return None


def detect_node_test_command(root: Path) -> str | None:
    pkg = root / "package.json"
    if not pkg.exists():
        return None
    import json

    try:
        data = json.loads(_read(pkg))
    except json.JSONDecodeError:
        return None
    scripts = data.get("scripts") or {}
    if "vitest" in str(scripts) or (root / "vitest.config.ts").exists():
        return "npx vitest run"
    if "test" in scripts:
        return "npm test"
    return None


def detect_python_test_command(root: Path) -> str | None:
    pyproject = (root / "pyproject.toml").exists()
    if (root / "pytest.ini").exists() or ((root / "tests").is_dir() and pyproject):
        return "uv run pytest" if pyproject else "pytest"
    return None


def detect_rust_test_command(root: Path) -> str | None:
    if (root / "Cargo.toml").exists():
        return "cargo test"
    return None


def detect_test_command(root: Path) -> str | None:
    for detector in (
        detect_php_test_command,
        detect_node_test_command,
        detect_python_test_command,
        detect_rust_test_command,
    ):
        cmd = detector(root)
        if cmd:
            return cmd
    return None


def detect_lint_command(root: Path) -> str | None:
    if (root / "composer.json").exists():
        if (root / "pint.json").exists() or (root / ".php-cs-fixer.php").exists():
            return "./vendor/bin/pint --test"
        return "./vendor/bin/pint" if (root / "pint.json").exists() else None
    if (root / "package.json").exists():
        return "npm run lint"
    if (root / "pyproject.toml").exists():
        return "uv run ruff check ."
    if (root / "Cargo.toml").exists():
        return "cargo clippy"
    return None


def detect_build_command(root: Path) -> str | None:
    if (root / "Cargo.toml").exists():
        return "cargo build"
    if (root / "package.json").exists():
        return "npm run build"
    if (root / "composer.json").exists():
        return "composer install --no-interaction"
    return None


def find_secrets(root: Path) -> list[str]:
    hits: list[str] = []
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if SECRET_FILES.search(rel):
            hits.append(rel)
    return hits[:50]


def find_key_dirs(root: Path) -> list[str]:
    candidates = [
        "src", "app", "lib", "tests", "test", "resources", "config", "database",
        "routes", "components", "modules", "packages", "internal", "cmd", "docs",
    ]
    return [c for c in candidates if (root / c).is_dir()]


def detect_signals(root: Path, profile: ProjectProfile) -> list[Signal]:
    signals: list[Signal] = []
    if (root / "AGENTS.md").exists():
        signals.append(Signal("docs", "AGENTS.md", "Guia de agentes existente: reutilizarla en los prompts"))
    for candidate in ("CLAUDE.md", "CONTRIBUTING.md", ".cursorrules"):
        if (root / candidate).exists():
            signals.append(Signal("docs", candidate, "Contexto del proyecto disponible"))
    if (root / "opencode.jsonc").exists() or (root / "opencode.json").exists():
        signals.append(Signal("config", "opencode.jsonc", "El proyecto ya tiene config de OpenCode"))
    agents_dir = root / ".opencode" / "agents"
    if agents_dir.is_dir():
        existing = sorted(p.stem for p in agents_dir.glob("*.md"))
        if existing:
            signals.append(Signal("agents", ".opencode/agents", f"Ya existen: {', '.join(existing)}"))
    if profile.test_command:
        signals.append(Signal("tests", "test", f"Comando de test detectado: {profile.test_command}"))
    if profile.secret_paths:
        signals.append(Signal("secrets", profile.secret_paths[0], f"{len(profile.secret_paths)} fichero(s) sensible(s)"))
    if profile.languages.get("php", 0) > 50:
        signals.append(Signal("stack", "php", "Proyecto PHP mayoritario: espera tipado estricto y PSR"))
    return signals


def scan_project(root: Path) -> ProjectProfile:
    root = root.resolve()
    profile = ProjectProfile(
        root=root,
        name=root.name,
        languages=count_languages(root),
        frameworks=detect_frameworks(root),
        test_command=detect_test_command(root),
        lint_command=detect_lint_command(root),
        build_command=detect_build_command(root),
        secret_paths=find_secrets(root),
        key_dirs=find_key_dirs(root),
    )
    profile.file_count = sum(profile.languages.values())
    profile.is_monorepo = len(
        [d for d in ("packages", "apps", "modules") if (root / d).is_dir()]
    ) > 0
    profile.signals = detect_signals(root, profile)
    return profile