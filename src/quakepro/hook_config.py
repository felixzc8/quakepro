"""Provider-neutral hook setup and command routing."""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import shutil
import sys
from typing import Callable, Union

from . import claude_hooks, codex_hooks, pi_hooks


PROVIDERS = ("claude", "codex", "pi")
Executable = Union[str, Callable[[], str]]


def _resolve_executable(executable: Executable) -> str:
    return executable() if callable(executable) else executable


def _hook_command(executable: str, provider: str) -> str:
    return shlex.join([executable, "_hook", provider])


def _configure(
    provider: str,
    operation: str,
    executable: str,
    scope: str,
    root: str | None,
) -> tuple[bool, Path, Path | None]:
    if provider == "pi":
        changed, path = pi_hooks.configure(operation, executable, scope, root)
        return changed, path, None
    command = _hook_command(executable, provider)
    if provider == "claude":
        changed, path = claude_hooks.configure(operation, command, scope, root)
        return changed, path, None
    return codex_hooks.configure(operation, command, scope, root)


def _run(
    providers: tuple[str, ...],
    operation: str,
    executable: str,
    scope: str,
    root: str | None,
) -> int:
    failed = False
    for provider in providers:
        try:
            changed, path, config_path = _configure(
                provider, operation, executable, scope, root,
            )
        except (
            claude_hooks.HookConfigError,
            codex_hooks.HookConfigError,
            pi_hooks.HookConfigError,
            OSError,
        ) as exc:
            print(f"quakepro {provider} hooks: {exc}", file=sys.stderr)
            failed = True
            continue
        state = "Installed" if operation == "install" else "Removed"
        if not changed:
            state = "Already installed" if operation == "install" else "Already absent"
        print(f"{state} {provider.title()} hooks: {path}")
        if operation == "install" and provider == "claude":
            print("Restart Claude Code or open /hooks to reload QuakePro hooks.")
        if operation == "install" and provider == "codex":
            print("Restart Codex, then open /hooks to review and trust QuakePro hooks.")
        if operation == "install" and provider == "pi":
            print("Restart pi or run /reload to load QuakePro extension.")
        if operation == "install" and scope == "project":
            print(f"Trust project before {provider.title()} loads project hooks.")
        if config_path is not None and codex_hooks.has_inline_hooks(config_path):
            print(
                f"Warning: {config_path} also defines inline hooks; "
                "Codex loads both sources and warns.",
                file=sys.stderr,
            )
    return 1 if failed else 0


def _doctor(
    providers: tuple[str, ...],
    executable: str,
    scope: str,
    root: str | None,
) -> int:
    failed = False
    for provider in providers:
        try:
            if provider == "claude":
                path, problems = claude_hooks.diagnose(
                    _hook_command(executable, provider), scope, root,
                )
            elif provider == "codex":
                path, problems = codex_hooks.diagnose(
                    _hook_command(executable, provider), scope, root,
                )
            else:
                path, problems = pi_hooks.diagnose(executable, scope, root)
        except (
            claude_hooks.HookConfigError,
            codex_hooks.HookConfigError,
            pi_hooks.HookConfigError,
            OSError,
        ) as exc:
            print(f"{provider.title()} hooks need setup: {exc}", file=sys.stderr)
            failed = True
            continue
        if problems:
            joined = ", ".join(problems)
            print(
                f"{provider.title()} hooks need setup: {path} ({joined})",
                file=sys.stderr,
            )
            failed = True
        else:
            print(f"OK {provider.title()} hooks: {path}")
    return 1 if failed else 0


def hooks_main(argv: list[str], executable: Executable) -> int:
    parser = argparse.ArgumentParser(prog="quakepro hooks")
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--provider", choices=PROVIDERS, default="codex")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    return _run(
        (args.provider,), args.operation, _resolve_executable(executable),
        args.scope, args.root,
    )


def _detected(which: Callable[[str], str | None] = shutil.which) -> tuple[str, ...]:
    return tuple(provider for provider in PROVIDERS if which(provider))


def setup_main(argv: list[str], executable: Executable) -> int:
    parser = argparse.ArgumentParser(prog="quakepro setup")
    parser.add_argument("--provider", choices=(*PROVIDERS, "all"))
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    if args.provider == "all":
        providers = PROVIDERS
    elif args.provider:
        providers = (args.provider,)
    else:
        providers = _detected()
        if not providers:
            print("quakepro setup: no Claude Code, Codex, or pi command found")
            return 1
    resolved = _resolve_executable(executable)
    status = _run(providers, "install", resolved, args.scope, args.root)
    if status:
        return status
    return _doctor(providers, resolved, args.scope, args.root)


def doctor_main(argv: list[str], executable: Executable) -> int:
    parser = argparse.ArgumentParser(prog="quakepro doctor")
    parser.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    return _doctor(
        providers, _resolve_executable(executable), args.scope, args.root,
    )
