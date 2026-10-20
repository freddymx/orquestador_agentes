"""Sprint 4: deteccion real de stack, frameworks y comandos del proyecto."""

import json

from orquestador.scan import (
    detect_build_command,
    detect_lint_command,
    detect_stack,
    detect_test_command,
    scan_project,
)


def _write(root, name, text=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _composer(root, require=None, require_dev=None, scripts=None):
    data = {}
    if require is not None:
        data["require"] = require
    if require_dev is not None:
        data["require-dev"] = require_dev
    if scripts is not None:
        data["scripts"] = scripts
    _write(root, "composer.json", json.dumps(data))


def _package(root, dependencies=None, dev_dependencies=None, scripts=None):
    data = {}
    if dependencies is not None:
        data["dependencies"] = dependencies
    if dev_dependencies is not None:
        data["devDependencies"] = dev_dependencies
    if scripts is not None:
        data["scripts"] = scripts
    _write(root, "package.json", json.dumps(data))


# --------------------------------------------------------------------------- #
# frameworks: solo lo demostrable
# --------------------------------------------------------------------------- #


def test_generic_composer_project_is_not_laravel(tmp_path):
    _composer(tmp_path, require={"acme/pkg": "^1.0"})
    _write(tmp_path, "src/Foo.php", "<?php class Foo {}")

    profile = scan_project(tmp_path)
    assert profile.frameworks == []
    assert profile.runtimes == ["php"]
    assert profile.stack_label == "php"


def test_laravel_from_composer_dependency(tmp_path):
    _composer(tmp_path, require={"laravel/framework": "^12.0"})
    _write(tmp_path, "app/Models/User.php", "<?php class User {}")

    profile = scan_project(tmp_path)
    assert "laravel" in profile.frameworks
    assert profile.stack_label == "php/laravel"


def test_laravel_from_artisan_only(tmp_path):
    _write(tmp_path, "artisan", "#!/usr/bin/env php\n")
    _write(tmp_path, "app/Models/User.php", "<?php class User {}")

    profile = scan_project(tmp_path)
    assert "laravel" in profile.frameworks


def test_next_with_pnpm_uses_scripts_and_lockfile(tmp_path):
    _package(
        tmp_path,
        dependencies={"next": "14", "react": "18"},
        scripts={"test": "vitest run", "lint": "eslint .", "build": "next build"},
    )
    _write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    _write(tmp_path, "src/app.ts", "export const app = 1")

    profile = scan_project(tmp_path)
    assert "node" in profile.runtimes
    assert profile.frameworks == ["next", "react"]
    assert profile.test_command == "npx vitest run"
    assert profile.lint_command == "pnpm run lint"
    assert profile.build_command == "pnpm run build"


def test_node_ignores_placeholder_test_and_detects_eslint(tmp_path):
    _package(
        tmp_path,
        dev_dependencies={"eslint": "^9"},
        scripts={"test": 'echo "Error: no test specified" && exit 1'},
    )
    _write(tmp_path, ".eslintrc.json", "{}")

    assert detect_test_command(tmp_path) is None
    assert detect_lint_command(tmp_path) == "npx eslint ."
    assert detect_build_command(tmp_path) is None


def test_node_runner_without_script_is_detected_from_dependency(tmp_path):
    _package(tmp_path, dev_dependencies={"vitest": "^3"}, scripts={"build": "tsc"})
    assert detect_test_command(tmp_path) == "npx vitest run"
    assert detect_lint_command(tmp_path) is None
    assert detect_build_command(tmp_path) == "npm run build"


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


def test_fastapi_with_uv_pytest_and_ruff(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        "[project]\n"
        'name = "api"\n'
        'dependencies = ["fastapi>=0.110", "uvicorn[standard]"]\n'
        "\n[dependency-groups]\n"
        'dev = ["pytest>=8", "ruff>=0.6"]\n'
        "\n[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        "\n[tool.ruff]\n"
        "line-length = 100\n",
    )
    _write(tmp_path, "uv.lock", "version = 1\n")
    _write(tmp_path, "app/main.py", "from fastapi import FastAPI\n")

    profile = scan_project(tmp_path)
    assert "python" in profile.runtimes
    assert profile.frameworks == ["fastapi"]
    assert profile.test_command == "uv run pytest"
    assert profile.lint_command == "uv run ruff check ."


def test_django_requirements_without_uv(tmp_path):
    _write(tmp_path, "requirements.txt", "# prod\nDjango==5.0\npytest>=8\n")
    _write(tmp_path, "tests/test_views.py", "def test_ok(): pass\n")

    profile = scan_project(tmp_path)
    assert profile.frameworks == ["django"]
    assert profile.test_command == "python -m pytest"
    assert profile.lint_command is None


def test_python_without_pytest_falls_back_to_unittest(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\nname = "lib"\n')
    _write(tmp_path, "tests/test_lib.py", "def test_ok(): pass\n")

    profile = scan_project(tmp_path)
    assert profile.test_command == "python -m unittest discover"


def test_python_detects_flake8_when_ruff_is_absent(tmp_path):
    _write(tmp_path, "requirements.txt", "flask\nflake8\n")
    _write(tmp_path, ".flake8", "[flake8]\nmax-line-length = 100\n")

    profile = scan_project(tmp_path)
    assert profile.frameworks == ["flask"]
    assert profile.lint_command == "flake8 ."


# --------------------------------------------------------------------------- #
# PHP, Go y Makefile
# --------------------------------------------------------------------------- #


def test_php_scripts_win_over_binaries(tmp_path):
    _composer(
        tmp_path,
        require={"laravel/framework": "^12.0"},
        scripts={"test": "phpunit", "lint": "pint"},
    )
    _write(tmp_path, "phpunit.xml", "<phpunit/>")

    assert detect_test_command(tmp_path) == "composer test"
    assert detect_lint_command(tmp_path) == "composer lint"


def test_pest_is_detected(tmp_path):
    _composer(tmp_path, require_dev={"pestphp/pest": "^3.0"})
    _write(tmp_path, "pest.php", "<?php\n")

    assert detect_test_command(tmp_path) == "./vendor/bin/pest"


def test_pint_is_detected_from_config(tmp_path):
    _composer(tmp_path, require={"laravel/framework": "^12.0"})
    _write(tmp_path, "pint.json", "{}")

    assert detect_lint_command(tmp_path) == "./vendor/bin/pint --test"


def test_go_stack_and_commands(tmp_path):
    _write(tmp_path, "go.mod", "module example.com/api\n\nrequire github.com/gin-gonic/gin v1.10.0\n")
    _write(tmp_path, "main.go", "package main\n")

    profile = scan_project(tmp_path)
    assert "go" in profile.runtimes
    assert profile.frameworks == ["gin"]
    assert profile.test_command == "go test ./..."
    assert profile.lint_command == "go vet ./..."
    assert profile.build_command == "go build ./..."


def test_makefile_is_last_resort(tmp_path):
    _write(tmp_path, "main.c", "int main(void) { return 0; }\n")
    _write(tmp_path, "Makefile", "test:\n\t./run-tests\n\nlint:\n\tcc -Wall\n\nbuild:\n\tcc main.c\n")

    profile = scan_project(tmp_path)
    assert profile.test_command == "make test"
    assert profile.lint_command == "make lint"
    assert profile.build_command == "make build"


def test_docker_and_ci_are_signals_not_frameworks(tmp_path):
    _write(tmp_path, "docker-compose.yml", "services: {}\n")
    _write(tmp_path, ".github/workflows/ci.yml", "name: CI\n")

    profile = scan_project(tmp_path)
    assert profile.frameworks == []
    assert {signal.kind for signal in profile.signals} >= {"infra", "ci"}


def test_detect_stack_returns_runtimes_and_frameworks_separately(tmp_path):
    _package(tmp_path, dependencies={"express": "^4"})
    runtimes, frameworks = detect_stack(tmp_path)
    assert runtimes == ["node"]
    assert frameworks == ["express"]
