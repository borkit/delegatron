"""Per-language sandbox toolchains: test/install commands and Docker images.

Shared by project detection, the sandbox factory and the sandboxes so a
language gets the same commands and image wherever it is resolved.  Every
command here is infrastructure (trusted), never agent output.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

from delegatron.config import DOCKER_IMAGES

PYTEST_COMMAND = "python -m pytest -v"
PYTEST_INSTALL = "pip install pytest -q"
PIPENV_INSTALL: tuple[str, ...] = ("pip install pipenv -q", "pipenv install --dev --system")
# ``-CI`` makes Invoke-Pester exit non-zero on failures; double quotes work
# under both bash and cmd.exe.
PESTER_TEST_COMMAND = 'pwsh -NoProfile -Command "Invoke-Pester -CI -Output Detailed"'
# Container images: installed for every user of the image.
PESTER_INSTALL = 'pwsh -NoProfile -Command "Install-Module Pester -Force -Scope AllUsers"'
# Sandboxes without root (local host, E2B): Pester 5 for the current user,
# only when it is missing.  ``-SkipPublisherCheck`` lets it sit beside the
# Pester 3 that Windows ships.
PESTER_INSTALL_CURRENT_USER = (
    'pwsh -NoProfile -Command "if (-not (Get-Module -ListAvailable Pester'
    " | Where-Object Version -ge '5.0')) { Install-Module Pester -Force"
    ' -Scope CurrentUser -SkipPublisherCheck }"'
)
# The build runs with the tests: the build dir lives in the per-run workspace copy.
CMAKE_TEST_COMMAND = (
    "cmake -B build && cmake --build build && ctest --test-dir build --no-tests=error"
)
# Debian-based images only (apt); skipped when the image already has cmake.
CMAKE_TOOLCHAIN_INSTALL = (
    "command -v cmake >/dev/null 2>&1 || "
    "(apt-get update -qq && apt-get install -y -qq cmake g++ make)"
)

# Install steps that need root in a disposable Linux container.  Sandboxes
# that run on a machine Delegatron does not own (local host, E2B) drop them
# (``None``) or use their per-user form.
_HOST_INSTALLS: dict[str, str | None] = {
    CMAKE_TOOLCHAIN_INSTALL: None,
    PESTER_INSTALL: PESTER_INSTALL_CURRENT_USER,
}
# Fills $CARGO_HOME/registry while the install phase has network.
CARGO_FETCH = "cargo fetch"
DOTNET_TEST_COMMAND = "dotnet test --verbosity normal"


class LangRule(NamedTuple):
    """A file pattern that identifies a language and its sandbox commands.

    Auxiliary rules (``primary=False``) match files that also turn up in
    projects of other languages (scripts, Makefiles); they only decide the
    language when nothing primary matched.
    """

    pattern: str
    language: str
    test_command: str
    install_command: str
    primary: bool = True


LANG_RULES: tuple[LangRule, ...] = (
    # Python — manifest installs (pip -r / -e / pipenv) are added by the sandbox.
    LangRule("pyproject.toml", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    LangRule("setup.py", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    LangRule("setup.cfg", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    LangRule("requirements.txt", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    LangRule("Pipfile", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    LangRule("*.py", "python", PYTEST_COMMAND, PYTEST_INSTALL),
    # .NET
    LangRule("*.sln", "csharp", DOTNET_TEST_COMMAND, "dotnet restore"),
    LangRule("*.csproj", "csharp", DOTNET_TEST_COMMAND, "dotnet restore"),
    LangRule("*.fsproj", "fsharp", DOTNET_TEST_COMMAND, "dotnet restore"),
    LangRule("*.cs", "csharp", DOTNET_TEST_COMMAND, "dotnet restore"),
    # Go — -v so --- PASS/FAIL lines print for per-test counting
    LangRule("go.mod", "go", "go test -v ./...", "go mod download"),
    LangRule("Cargo.toml", "rust", "cargo test", CARGO_FETCH),
    # ``dependency:go-offline`` also fetches the build plugins (surefire,
    # compiler) so the test phase can run with --network none.
    LangRule("pom.xml", "java", "mvn test -q", "mvn dependency:go-offline -q"),
    LangRule("build.gradle", "java", "gradle test", "gradle dependencies"),
    LangRule("build.gradle.kts", "kotlin", "gradle test", "gradle dependencies"),
    LangRule("package.json", "javascript", "npm test", "npm install"),
    LangRule("Gemfile", "ruby", "bundle exec rspec", "bundle install"),
    LangRule("composer.json", "php", "vendor/bin/phpunit", "composer install"),
    LangRule("CMakeLists.txt", "cpp", CMAKE_TEST_COMMAND, CMAKE_TOOLCHAIN_INSTALL),
    LangRule("Package.swift", "swift", "swift test", "swift package resolve"),
    LangRule("pubspec.yaml", "dart", "dart test", "dart pub get"),
    LangRule("mix.exs", "elixir", "mix test", "mix deps.get"),
    LangRule("build.sbt", "scala", "sbt test", "sbt update"),
    LangRule("stack.yaml", "haskell", "stack test", "stack build --only-dependencies"),
    # Auxiliary
    LangRule("*.ps1", "powershell", PESTER_TEST_COMMAND, PESTER_INSTALL, primary=False),
    LangRule("*.sh", "shell", "bash -n *.sh", "", primary=False),
    LangRule("Makefile", "c", "make test", "make", primary=False),
)

# Names project detection uses for a language that LANG_RULES spells differently.
_LANGUAGE_ALIASES: dict[str, str] = {
    "c++": "cpp",
    "c#": "csharp",
    "dotnet": "csharp",
    "f#": "fsharp",
    "node": "javascript",
    "typescript": "javascript",
}

# Detected language → DOCKER_IMAGES key.
_LANGUAGE_IMAGE_KEY: dict[str, str] = {
    "python": "python",
    "javascript": "node",
    "go": "go",
    "rust": "rust",
    "csharp": "dotnet",
    "fsharp": "dotnet",
    "java": "java",
    "kotlin": "java",
    "ruby": "ruby",
    "php": "php",
    "powershell": "powershell",
    "cpp": "cpp",
    "c": "cpp",
}

# Toolchain images that config.DOCKER_IMAGES does not list.
_TOOLCHAIN_IMAGES: dict[str, str] = {
    "powershell": "mcr.microsoft.com/powershell:7.4-ubuntu-22.04",
    # Debian-based with gcc, g++ and make; cmake comes from the install step.
    "cpp": "gcc:14",
}


def canonical_language(language: str) -> str:
    """Normalize a detected language name to the spelling LANG_RULES uses."""
    name = language.strip().lower()
    return _LANGUAGE_ALIASES.get(name, name)


def rule_for_language(language: str) -> LangRule | None:
    """Return the first rule for *language*, or ``None`` if it has none."""
    lang = canonical_language(language)
    return next((rule for rule in LANG_RULES if rule.language == lang), None)


def match_file(path: str) -> LangRule | None:
    """Return the first rule whose pattern matches the file name of *path*."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return next((rule for rule in LANG_RULES if fnmatchcase(name, rule.pattern)), None)


def host_install_commands(commands: Iterable[str]) -> list[str]:
    """*commands* fit for a sandbox without root on a machine Delegatron does not own.

    System package installs meant for a disposable container (apt-get for
    CMake) are dropped; module installs go to the current user.
    """
    host: list[str] = []
    for command in commands:
        replacement = _HOST_INSTALLS.get(command, command)
        if replacement:
            host.append(replacement)
    return host


def is_container_only_install(command: str) -> bool:
    """Whether *command* installs system packages and needs a Debian-based container."""
    return command in _HOST_INSTALLS and _HOST_INSTALLS[command] is None


def select_docker_image(language: str) -> str:
    """Return the well-known Docker image for a detected language.

    Empty string if no mapping exists (caller should use the sandbox default).
    """
    key = _LANGUAGE_IMAGE_KEY.get(canonical_language(language)) if language else None
    if not key:
        return ""
    return DOCKER_IMAGES.get(key) or _TOOLCHAIN_IMAGES.get(key, "")


def pyproject_has_test_extra(pyproject: Path) -> bool:
    """Whether *pyproject* declares a ``test`` optional-dependencies extra."""
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "[project.optional-dependencies]" in text and ("\ntest " in text or "\ntest=" in text)
