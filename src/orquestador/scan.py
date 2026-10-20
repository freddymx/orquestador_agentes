"""Escaneo del proyecto: stack, comandos, carpetas sensibles y perfil de trabajo."""

from __future__ import annotations

import json
import re
import tomllib
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
    runtimes: list[str] = field(default_factory=list)
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
        language = self.primary_language
        if language == "desconocido" and self.runtimes:
            language = self.runtimes[0]
        if not self.frameworks:
            return language
        return f"{language}/{'+'.join(self.frameworks)}"

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


# --------------------------------------------------------------------------- #
# Lectura de manifiestos
# --------------------------------------------------------------------------- #


def _read(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _toml_file(path: Path) -> dict:
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _dep_name(spec: str) -> str | None:
    text = spec.strip()
    if not text or text.startswith(("#", "-", "git+", "http:", "https:")):
        return None
    name = re.split(r"[<>=!~;\[\]\s@]", text, maxsplit=1)[0].strip().lower()
    return name.replace("_", "-") or None


def _collect_strings(values, out: set[str]) -> None:
    if not isinstance(values, (list, tuple)):
        return
    for value in values:
        if isinstance(value, str):
            name = _dep_name(value)
            if name:
                out.add(name)


def _collect_keys(values, out: set[str]) -> None:
    if isinstance(values, dict):
        for key in values:
            name = str(key).strip().lower()
            if name and name != "python":
                out.add(name.replace("_", "-"))


def _composer(root: Path) -> tuple[dict, dict[str, str]]:
    data = _json_file(root / "composer.json")
    packages: dict[str, str] = {}
    for section in ("require", "require-dev"):
        value = data.get(section)
        if isinstance(value, dict):
            packages.update({str(k).lower(): str(v) for k, v in value.items()})
    return data, packages


def _node(root: Path) -> tuple[dict, dict[str, str], dict[str, str]]:
    data = _json_file(root / "package.json")
    deps: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = data.get(section)
        if isinstance(value, dict):
            deps.update({str(k).lower(): str(v) for k, v in value.items()})
    scripts = data.get("scripts")
    clean_scripts = (
        {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}
    )
    return data, deps, clean_scripts


def _is_python_project(root: Path) -> bool:
    return any(
        (root / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile")
    )


def _python_packages(root: Path) -> set[str]:
    names: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        data = _toml_file(pyproject)
        raw_project = data.get("project")
        project = raw_project if isinstance(raw_project, dict) else {}
        _collect_strings(project.get("dependencies"), names)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                _collect_strings(group, names)
        groups = data.get("dependency-groups")
        if isinstance(groups, dict):
            for group in groups.values():
                _collect_strings(group, names)
        poetry = None
        tool = data.get("tool")
        if isinstance(tool, dict):
            candidate = tool.get("poetry")
            poetry = candidate if isinstance(candidate, dict) else None
        if isinstance(poetry, dict):
            _collect_keys(poetry.get("dependencies"), names)
            _collect_keys(poetry.get("dev-dependencies"), names)
            for group in (poetry.get("group") or {}).values():
                if isinstance(group, dict):
                    _collect_keys(group.get("dependencies"), names)
    pipfile = root / "Pipfile"
    if pipfile.exists():
        data = _toml_file(pipfile)
        _collect_keys(data.get("packages"), names)
        _collect_keys(data.get("dev-packages"), names)
    for req in sorted(root.glob("requirements*.txt")):
        for line in _read(req).splitlines():
            name = _dep_name(line)
            if name:
                names.add(name)
    return names


def _pyproject_has(root: Path, dotted: str) -> bool:
    cursor = _toml_file(root / "pyproject.toml")
    for part in dotted.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def _uses_uv(root: Path) -> bool:
    return (root / "uv.lock").exists() or (root / "uv.toml").exists()


# --------------------------------------------------------------------------- #
# Stack: runtimes y frameworks reales
# --------------------------------------------------------------------------- #

PHP_FRAMEWORKS = (
    ("laravel/framework", "laravel"),
    ("symfony/framework-bundle", "symfony"),
    ("slim/slim", "slim"),
    ("cakephp/cakephp", "cakephp"),
    ("codeigniter4/framework", "codeigniter"),
    ("yiisoft/yii2", "yii"),
)

NODE_FRAMEWORKS = (
    ("next", "next"),
    ("nuxt", "nuxt"),
    ("@angular/core", "angular"),
    ("@sveltejs/kit", "sveltekit"),
    ("svelte", "svelte"),
    ("astro", "astro"),
    ("@remix-run/react", "remix"),
    ("solid-js", "solid"),
    ("react", "react"),
    ("vue", "vue"),
    ("@nestjs/core", "nestjs"),
    ("express", "express"),
    ("fastify", "fastify"),
    ("electron", "electron"),
    ("expo", "expo"),
    ("react-native", "react-native"),
)

PYTHON_FRAMEWORKS = (
    ("django", "django"),
    ("flask", "flask"),
    ("fastapi", "fastapi"),
    ("starlette", "starlette"),
    ("litestar", "litestar"),
    ("streamlit", "streamlit"),
)

RUST_FRAMEWORKS = (
    ("tauri", "tauri"),
    ("axum", "axum"),
    ("actix-web", "actix"),
    ("rocket", "rocket"),
    ("leptos", "leptos"),
    ("dioxus", "dioxus"),
    ("bevy", "bevy"),
)

GO_FRAMEWORKS = (
    ("github.com/gin-gonic/gin", "gin"),
    ("github.com/labstack/echo", "echo"),
    ("github.com/gofiber/fiber", "fiber"),
    ("github.com/go-chi/chi", "chi"),
    ("github.com/gorilla/mux", "gorilla"),
)

RUBY_FRAMEWORKS = (
    ("rails", "rails"),
    ("sinatra", "sinatra"),
    ("hanami", "hanami"),
)


def _detect_php_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    if not any(
        (root / name).exists() for name in ("composer.json", "pest.php", "artisan")
    ):
        return
    _add(runtimes, "php")
    _, packages = _composer(root)
    for package, name in PHP_FRAMEWORKS:
        if package in packages:
            _add(frameworks, name)
    if "laravel" not in frameworks and (root / "artisan").exists():
        _add(frameworks, "laravel")


def _detect_node_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    if not (root / "package.json").exists():
        return
    _add(runtimes, "node")
    _, deps, _ = _node(root)
    for package, name in NODE_FRAMEWORKS:
        if package in deps:
            _add(frameworks, name)


def _detect_python_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    if not _is_python_project(root):
        return
    _add(runtimes, "python")
    packages = _python_packages(root)
    for package, name in PYTHON_FRAMEWORKS:
        if package in packages:
            _add(frameworks, name)


def _detect_rust_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    cargo = root / "Cargo.toml"
    if not cargo.exists():
        return
    _add(runtimes, "rust")
    deps = _toml_file(cargo).get("dependencies")
    if isinstance(deps, dict):
        present = {str(key).lower() for key in deps}
        for package, name in RUST_FRAMEWORKS:
            if package in present:
                _add(frameworks, name)


def _detect_go_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    gomod = root / "go.mod"
    if not gomod.exists():
        return
    _add(runtimes, "go")
    text = _read(gomod)
    for module, name in GO_FRAMEWORKS:
        if module in text:
            _add(frameworks, name)


def _detect_ruby_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    gemfile = root / "Gemfile"
    if not gemfile.exists():
        return
    _add(runtimes, "ruby")
    text = _read(gemfile)
    for gem, name in RUBY_FRAMEWORKS:
        if re.search(rf"^\s*gem\s+['\"]{re.escape(gem)}['\"]", text, re.MULTILINE):
            _add(frameworks, name)


def _detect_jvm_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    if (root / "pom.xml").exists():
        _add(runtimes, "java")
        if "spring-boot" in _read(root / "pom.xml"):
            _add(frameworks, "spring")
        return
    gradle = next(
        (path for path in ("build.gradle.kts", "build.gradle") if (root / path).exists()), None
    )
    if gradle is None:
        return
    _add(runtimes, "kotlin" if (root / "src" / "main" / "kotlin").is_dir() else "java")
    if "org.springframework.boot" in _read(root / gradle):
        _add(frameworks, "spring")
    if "com.android.application" in _read(root / gradle):
        _add(frameworks, "android")


def _dotnet_files(root: Path) -> list[Path]:
    suffixes = (".csproj", ".fsproj", ".sln")
    return [path for path in iter_files(root) if path.suffix.lower() in suffixes]


def _detect_dotnet_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    files = _dotnet_files(root)
    if not files:
        return
    _add(runtimes, "fsharp" if any(path.suffix == ".fsproj" for path in files) else "csharp")
    if any("Microsoft.NET.Sdk.Web" in _read(path) for path in files if path.suffix != ".sln"):
        _add(frameworks, "aspnet")


def _detect_swift_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    package = root / "Package.swift"
    if not package.exists():
        return
    _add(runtimes, "swift")
    if "vapor" in _read(package).lower():
        _add(frameworks, "vapor")


def _detect_elixir_stack(root: Path, runtimes: list[str], frameworks: list[str]) -> None:
    mix = root / "mix.exs"
    if not mix.exists():
        return
    _add(runtimes, "elixir")
    if ":phoenix" in _read(mix):
        _add(frameworks, "phoenix")


def detect_stack(root: Path) -> tuple[list[str], list[str]]:
    """Devuelve (runtimes, frameworks). Un runtime no es un framework.

    Antes cualquier `composer.json` se etiquetaba como Laravel; ahora Laravel exige
    `laravel/framework` o un `artisan` real. Los frameworks son solo los que se pueden
    demostrar desde el manifiesto, no la herramienta de build ni Docker.
    """
    runtimes: list[str] = []
    frameworks: list[str] = []
    _detect_php_stack(root, runtimes, frameworks)
    _detect_node_stack(root, runtimes, frameworks)
    _detect_python_stack(root, runtimes, frameworks)
    _detect_rust_stack(root, runtimes, frameworks)
    _detect_go_stack(root, runtimes, frameworks)
    _detect_ruby_stack(root, runtimes, frameworks)
    _detect_jvm_stack(root, runtimes, frameworks)
    _detect_dotnet_stack(root, runtimes, frameworks)
    _detect_swift_stack(root, runtimes, frameworks)
    _detect_elixir_stack(root, runtimes, frameworks)
    return runtimes, frameworks


def detect_frameworks(root: Path) -> list[str]:
    return detect_stack(root)[1]


# --------------------------------------------------------------------------- #
# Comandos reales: scripts del proyecto antes que heuristics
# --------------------------------------------------------------------------- #


def _php_uses_pest(root: Path) -> bool:
    _, packages = _composer(root)
    return (root / "pest.php").exists() or "pestphp/pest" in packages


def detect_php_test_command(root: Path) -> str | None:
    if not (
        (root / "composer.json").exists()
        or (root / "phpunit.xml").exists()
        or (root / "phpunit.xml.dist").exists()
        or (root / "pest.php").exists()
    ):
        return None
    scripts = _composer(root)[0].get("scripts")
    if isinstance(scripts, dict) and "test" in scripts:
        return "composer test"
    if (root / "phpunit.xml").exists() or (root / "phpunit.xml.dist").exists() or (root / "pest.php").exists():
        return "./vendor/bin/pest" if _php_uses_pest(root) else "./vendor/bin/phpunit"
    return None


def _node_run(root: Path, script: str) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return f"pnpm run {script}"
    if (root / "yarn.lock").exists():
        return f"yarn {script}"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return f"bun run {script}"
    return f"npm run {script}"


def _node_has_config(root: Path, *names: str) -> bool:
    return any((root / name).exists() for name in names)


def detect_node_test_command(root: Path) -> str | None:
    if not (root / "package.json").exists():
        return None
    _, deps, scripts = _node(root)
    script = scripts.get("test")
    if script:
        lowered = script.lower()
        if "no test specified" in lowered:
            script = ""
        elif "vitest" in lowered:
            return "npx vitest run"
        elif "jest" in lowered:
            return "npx jest"
        elif "playwright" in lowered:
            return "npx playwright test"
        elif "mocha" in lowered:
            return "npx mocha"
        else:
            return _node_run(root, "test")
    if "vitest" in deps or _node_has_config(
        root, "vitest.config.ts", "vitest.config.js", "vitest.config.mts"
    ):
        return "npx vitest run"
    if "jest" in deps or _node_has_config(
        root, "jest.config.ts", "jest.config.js", "jest.config.mjs", "jest.config.cjs"
    ):
        return "npx jest"
    if "@playwright/test" in deps or _node_has_config(
        root, "playwright.config.ts", "playwright.config.js"
    ):
        return "npx playwright test"
    if "mocha" in deps:
        return "npx mocha"
    return None


def _ini_has_section(path: Path, section: str) -> bool:
    if not path.exists():
        return False
    return re.search(rf"^\s*\[{re.escape(section)}\]", _read(path), re.MULTILINE) is not None


def detect_python_test_command(root: Path) -> str | None:
    if not _is_python_project(root):
        return None
    deps = _python_packages(root)
    pytest_config = (
        _pyproject_has(root, "tool.pytest.ini_options")
        or (root / "pytest.ini").exists()
        or (root / "conftest.py").exists()
        or _ini_has_section(root / "tox.ini", "pytest")
        or _ini_has_section(root / "setup.cfg", "tool:pytest")
    )
    if pytest_config or "pytest" in deps:
        return "uv run pytest" if _uses_uv(root) else "python -m pytest"
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return "python -m unittest discover"
    return None


def detect_rust_test_command(root: Path) -> str | None:
    return "cargo test" if (root / "Cargo.toml").exists() else None


def detect_go_test_command(root: Path) -> str | None:
    return "go test ./..." if (root / "go.mod").exists() else None


def detect_ruby_test_command(root: Path) -> str | None:
    if not (root / "Gemfile").exists():
        return None
    if (root / "spec").is_dir() or (root / ".rspec").exists():
        return "bundle exec rspec"
    if (root / "test").is_dir():
        return "bundle exec rake test"
    return None


def _gradle(root: Path) -> str:
    return "./gradlew" if (root / "gradlew").exists() else "gradle"


def detect_jvm_test_command(root: Path) -> str | None:
    if (root / "pom.xml").exists():
        return "mvn test"
    if (root / "build.gradle.kts").exists() or (root / "build.gradle").exists():
        return f"{_gradle(root)} test"
    return None


def detect_dotnet_test_command(root: Path) -> str | None:
    return "dotnet test" if _dotnet_files(root) else None


def detect_swift_test_command(root: Path) -> str | None:
    return "swift test" if (root / "Package.swift").exists() else None


def detect_elixir_test_command(root: Path) -> str | None:
    return "mix test" if (root / "mix.exs").exists() else None


def _make_target(root: Path, target: str) -> str | None:
    makefile = root / "Makefile"
    if not makefile.exists():
        return None
    pattern = rf"^{re.escape(target)}\s*:"
    return f"make {target}" if re.search(pattern, _read(makefile), re.MULTILINE) else None


def detect_test_command(root: Path) -> str | None:
    for detector in (
        detect_php_test_command,
        detect_node_test_command,
        detect_python_test_command,
        detect_rust_test_command,
        detect_go_test_command,
        detect_ruby_test_command,
        detect_jvm_test_command,
        detect_dotnet_test_command,
        detect_swift_test_command,
        detect_elixir_test_command,
    ):
        command = detector(root)
        if command:
            return command
    return _make_target(root, "test")


def detect_php_lint_command(root: Path) -> str | None:
    if not (root / "composer.json").exists():
        return None
    scripts = _composer(root)[0].get("scripts")
    if isinstance(scripts, dict) and "lint" in scripts:
        return "composer lint"
    _, packages = _composer(root)
    if (
        (root / "pint.json").exists()
        or "laravel/pint" in packages
    ):
        return "./vendor/bin/pint --test"
    if (
        (root / ".php-cs-fixer.php").exists()
        or (root / ".php-cs-fixer.dist.php").exists()
        or "friendsofphp/php-cs-fixer" in packages
    ):
        return "./vendor/bin/php-cs-fixer fix --dry-run --diff"
    if (
        (root / "phpcs.xml").exists()
        or (root / "phpcs.xml.dist").exists()
        or "squizlabs/php_codesniffer" in packages
    ):
        return "./vendor/bin/phpcs"
    return None


def detect_node_lint_command(root: Path) -> str | None:
    if not (root / "package.json").exists():
        return None
    _, deps, scripts = _node(root)
    if "lint" in scripts:
        return _node_run(root, "lint")
    if _node_has_config(root, "biome.json", "biome.jsonc"):
        return "npx biome check ."
    if "eslint" in deps or _node_has_config(
        root,
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.json",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.ts",
    ):
        return "npx eslint ."
    return None


def detect_python_lint_command(root: Path) -> str | None:
    if not _is_python_project(root):
        return None
    deps = _python_packages(root)
    ruff = (
        _pyproject_has(root, "tool.ruff")
        or (root / "ruff.toml").exists()
        or (root / ".ruff.toml").exists()
        or "ruff" in deps
    )
    if ruff:
        return "uv run ruff check ." if _uses_uv(root) else "ruff check ."
    flake8 = (
        (root / ".flake8").exists()
        or _ini_has_section(root / "setup.cfg", "flake8")
        or _ini_has_section(root / "tox.ini", "flake8")
        or "flake8" in deps
    )
    if flake8:
        return "flake8 ."
    pylint = (
        (root / ".pylintrc").exists()
        or (root / "pylintrc").exists()
        or _pyproject_has(root, "tool.pylint")
        or "pylint" in deps
    )
    if pylint:
        return "pylint ."
    return None


def detect_rust_lint_command(root: Path) -> str | None:
    return "cargo clippy" if (root / "Cargo.toml").exists() else None


def detect_go_lint_command(root: Path) -> str | None:
    return "go vet ./..." if (root / "go.mod").exists() else None


def detect_ruby_lint_command(root: Path) -> str | None:
    gemfile = root / "Gemfile"
    if not gemfile.exists():
        return None
    if (root / ".rubocop.yml").exists() or re.search(
        r"^\s*gem\s+['\"]rubocop['\"]", _read(gemfile), re.MULTILINE
    ):
        return "bundle exec rubocop"
    return None


def detect_jvm_lint_command(root: Path) -> str | None:
    if (root / "pom.xml").exists():
        text = _read(root / "pom.xml")
        if "checkstyle" in text or (root / "checkstyle.xml").exists():
            return "mvn checkstyle:check"
        return None
    gradle = next(
        (path for path in ("build.gradle.kts", "build.gradle") if (root / path).exists()), None
    )
    if gradle is not None and "spotless" in _read(root / gradle):
        return f"{_gradle(root)} spotlessCheck"
    return None


def detect_dotnet_lint_command(root: Path) -> str | None:
    if _dotnet_files(root) and (root / ".editorconfig").exists():
        return "dotnet format --verify-no-changes"
    return None


def detect_swift_lint_command(root: Path) -> str | None:
    if (root / ".swiftlint.yml").exists() or (root / ".swiftlint.yaml").exists():
        return "swiftlint"
    return None


def detect_elixir_lint_command(root: Path) -> str | None:
    return "mix format --check-formatted" if (root / "mix.exs").exists() else None


def detect_lint_command(root: Path) -> str | None:
    for detector in (
        detect_php_lint_command,
        detect_node_lint_command,
        detect_python_lint_command,
        detect_rust_lint_command,
        detect_go_lint_command,
        detect_ruby_lint_command,
        detect_jvm_lint_command,
        detect_dotnet_lint_command,
        detect_swift_lint_command,
        detect_elixir_lint_command,
    ):
        command = detector(root)
        if command:
            return command
    return _make_target(root, "lint")


def detect_php_build_command(root: Path) -> str | None:
    if not (root / "composer.json").exists():
        return None
    scripts = _composer(root)[0].get("scripts")
    if isinstance(scripts, dict) and "build" in scripts:
        return "composer build"
    return None


def detect_node_build_command(root: Path) -> str | None:
    if not (root / "package.json").exists():
        return None
    _, _, scripts = _node(root)
    return _node_run(root, "build") if "build" in scripts else None


def detect_rust_build_command(root: Path) -> str | None:
    return "cargo build" if (root / "Cargo.toml").exists() else None


def detect_go_build_command(root: Path) -> str | None:
    return "go build ./..." if (root / "go.mod").exists() else None


def detect_jvm_build_command(root: Path) -> str | None:
    if (root / "pom.xml").exists():
        return "mvn package -DskipTests"
    if (root / "build.gradle.kts").exists() or (root / "build.gradle").exists():
        return f"{_gradle(root)} assemble"
    return None


def detect_dotnet_build_command(root: Path) -> str | None:
    return "dotnet build" if _dotnet_files(root) else None


def detect_swift_build_command(root: Path) -> str | None:
    return "swift build" if (root / "Package.swift").exists() else None


def detect_elixir_build_command(root: Path) -> str | None:
    return "mix compile" if (root / "mix.exs").exists() else None


def detect_build_command(root: Path) -> str | None:
    for detector in (
        detect_php_build_command,
        detect_node_build_command,
        detect_rust_build_command,
        detect_go_build_command,
        detect_jvm_build_command,
        detect_dotnet_build_command,
        detect_swift_build_command,
        detect_elixir_build_command,
    ):
        command = detector(root)
        if command:
            return command
    return _make_target(root, "build")


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
    compose = next(
        (name for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml")
         if (root / name).exists()),
        None,
    )
    if compose:
        signals.append(Signal("infra", compose, "Contenedores definidos para el proyecto"))
    if (root / ".github" / "workflows").is_dir():
        signals.append(Signal("ci", ".github/workflows", "Integracion continua configurada"))
    if profile.languages.get("php", 0) > 50:
        signals.append(Signal("stack", "php", "Proyecto PHP mayoritario: espera tipado estricto y PSR"))
    return signals


def scan_project(root: Path) -> ProjectProfile:
    root = root.resolve()
    runtimes, frameworks = detect_stack(root)
    profile = ProjectProfile(
        root=root,
        name=root.name,
        languages=count_languages(root),
        runtimes=runtimes,
        frameworks=frameworks,
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
