#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Git-based MCP module installation handler.

Installs Python MCP modules from Git repositories into a controlled local
``install_target`` directory using ``pip install --target``, resolves the
commit SHA, loads the manifest, and provides version-check / refresh logic.

Key design decisions (see ``docs/GIT_MODULE_INSTALL_DEV_PLAN.md``):
- ``pip install --target`` is used so packages are isolated from the daemon's
  own environment.
- Install targets are commit-scoped:
  ``{Config.git_install_path}/{packageName}/{resolvedCommit}/``
- Refresh compares ``resolved_commit`` with the remote commit for the
  requested ref. No-op when unchanged.
- Git metadata is stored in ``MCPSetting.setting`` (no schema migration).
- ``GIT_TOKEN`` is never persisted to DB or logs.
"""
from __future__ import print_function

__author__ = "bibow"

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, quote

import pendulum
from graphene import ResolveInfo

from .config import Config
from .mcp_handlers import (
    _import_mcp_configuration_from_dir,
    _validate_package_name,
    load_mcp_configuration_into_models,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GIT_METADATA_KEYS = [
    "git_url",
    "git_ref",
    "git_subdirectory",
    "install_target",
    "install_mode",
    "version_strategy",
    "distribution_name",
    "installed_package_version",
    "latest_remote_version",
    "resolved_commit",
    "last_checked_commit",
    "last_checked_at",
    "installed_at",
]

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHORT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def _validate_git_url(git_url: str) -> str:
    """Validate a Git URL and return the normalized URL.

    Accepts HTTPS and SSH forms:
      - ``https://github.com/org/repo.git``
      - ``git@github.com:org/repo.git`` or ``ssh://git@github.com/org/repo.git``

    HTTPS is the default; SSH works when the host has a system SSH key
    (``~/.ssh``) or ``GIT_SSH_KEY`` is configured. Both schemes are subject
    to the host allowlist. Rejects ``file://`` and local paths.
    """
    if not git_url or not isinstance(git_url, str):
        raise Exception("gitUrl is required")

    # Normalize the scp-style shorthand (git@host:path) for validation, but
    # keep the caller's original URL — git and pip both understand it.
    normalized = git_url
    if git_url.startswith("git@"):
        normalized = f"ssh://{git_url.replace(':', '/', 1)}"
    elif git_url.startswith("ssh://"):
        normalized = git_url
    else:
        normalized = git_url

    parsed = urlparse(normalized)

    if parsed.scheme == "file" or not parsed.scheme:
        raise Exception(
            f"Unsupported Git URL scheme '{parsed.scheme or 'file'}': "
            f"only HTTPS and SSH Git URLs are allowed"
        )

    if parsed.scheme not in ("https", "ssh"):
        raise Exception(
            f"Unsupported Git URL scheme '{parsed.scheme}': "
            f"only HTTPS and SSH are allowed"
        )

    host = parsed.hostname or ""
    if not host:
        raise Exception(f"Invalid Git URL (no host): {git_url}")

    allowed_hosts = [
        h.strip().lower()
        for h in (Config.git_allowed_hosts or "").split(",")
        if h.strip()
    ]
    if allowed_hosts and host.lower() not in allowed_hosts:
        raise Exception(
            f"Git host '{host}' is not in the allowed list: {allowed_hosts}"
        )

    return git_url


# ---------------------------------------------------------------------------
# Git subprocess environment (SSH key handling)
# ---------------------------------------------------------------------------


def _git_ssh_command() -> Optional[str]:
    """Build a GIT_SSH_COMMAND string when GIT_SSH_KEY is configured.

    Writes the key material to a temp file with strict permissions, points
    ssh at it, and disables strict host-key prompting (the host must already
    be in known_hosts or StrictHostKeyChecking=accept-new accepts it on first
    use). Returns None when no key is configured — subprocesses then use the
    system SSH setup (~/.ssh, ssh-agent) as-is.
    """
    if not Config.git_ssh_key:
        return None

    key_path = os.path.join(Config.git_install_path, ".git_deploy_key")
    os.makedirs(Config.git_install_path, exist_ok=True)

    # Accept either a raw PEM key or an already-pem-encoded value with
    # literal "\n" escapes (common when the key is passed through an
    # env var / YAML single-line value).
    key_material = Config.git_ssh_key.replace("\\n", "\n")

    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key_material)
    os.chmod(key_path, 0o600)

    return (
        f'ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes '
        f"-o StrictHostKeyChecking=accept-new"
    )


def _git_env() -> Dict[str, str]:
    """Build the subprocess environment for git/pip calls.

    - Injects GIT_SSH_COMMAND when GIT_SSH_KEY is set (key material written
      to a 0600 file under git_install_path; never logged).
    - Otherwise inherits the process environment so system ssh keys
      (~/.ssh) and ssh-agent work as-is.
    """
    env = dict(os.environ)
    ssh_command = _git_ssh_command()
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    return env


# ---------------------------------------------------------------------------
# pip direct URL construction
# ---------------------------------------------------------------------------


def _authed_git_url(git_url: str) -> str:
    """Return a URL with the HTTPS token injected for private repos.

    Applies only to ``https://`` URLs; SSH URLs are returned unchanged
    (SSH auth comes from the system key or GIT_SSH_KEY, not a token).
    The token is never logged and never persisted.
    """
    if not Config.git_token:
        return git_url

    parsed = urlparse(git_url)
    if parsed.scheme != "https":
        # SSH URLs authenticate via SSH key; nothing to inject.
        return git_url

    return (
        f"{parsed.scheme}://x-access-token:{quote(Config.git_token, safe='')}"
        f"@{parsed.hostname}{parsed.path}"
    )


def _normalize_scp_url(git_url: str) -> str:
    """Convert an scp-style Git URL to the ssh:// form.

    pip's direct-URL grammar does not accept ``git+git@host:path``; it
    requires ``git+ssh://git@host/path``. ``git@host:path`` is kept for
    ls-remote (plain git accepts both).
    """
    if git_url.startswith("git@"):
        # git@github.com:org/repo.git -> ssh://git@github.com/org/repo.git
        return "ssh://" + git_url.replace(":", "/", 1)
    return git_url


def _build_pip_direct_url(
    git_url: str,
    git_ref: Optional[str] = None,
    git_subdirectory: Optional[str] = None,
) -> str:
    """Build a pip-compatible direct URL from URL, ref, and subdirectory.

    The HTTPS token (GIT_TOKEN) is injected here too, so private-repo
    installs authenticate the same way as ls-remote. Example:
        git+https://x-access-token:TOKEN@github.com/org/repo.git@v1.2.3#subdirectory=pkg
    """
    base = f"git+{_normalize_scp_url(_authed_git_url(git_url))}"
    if git_ref:
        base = f"{base}@{git_ref}"
    if git_subdirectory:
        base = f"{base}#subdirectory={git_subdirectory}"
    return base


# ---------------------------------------------------------------------------
# Install target path safety
# ---------------------------------------------------------------------------


def _install_target_path(package_name: str, resolved_commit: str) -> str:
    """Return the commit-scoped install target directory.

    ``{Config.git_install_path}/{packageName}/{resolvedCommit}/``
    """
    return os.path.join(
        Config.git_install_path, package_name, resolved_commit
    )


def _is_safe_path(path: str, parent: str) -> bool:
    """Return True if ``path`` is under ``parent`` (no traversal)."""
    try:
        real = os.path.realpath(path)
        parent_real = os.path.realpath(parent)
        return os.path.commonpath([real, parent_real]) == parent_real
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Commit resolution
# ---------------------------------------------------------------------------


def _resolve_remote_commit(
    git_url: str, git_ref: Optional[str], logger: Any = None
) -> str:
    """Resolve a ref (branch/tag/commit) to a commit SHA via ``git ls-remote``.

    If ``git_ref`` is already a 40-char SHA, return it directly without a
    network call.
    """
    if git_ref and _COMMIT_SHA_RE.match(git_ref):
        return git_ref

    log = logger or Config.logger

    ref = git_ref or "HEAD"
    cmd = ["git", "ls-remote", _authed_git_url(git_url), ref]

    env = _git_env()

    try:
        log.info(f"Resolving remote commit for ref '{ref}' ...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=Config.git_install_timeout,
            env=env,
        )
        if result.returncode != 0:
            raise Exception(
                f"git ls-remote failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        output = result.stdout.strip()
        if not output:
            raise Exception(
                f"git ls-remote returned no match for ref '{ref}'"
            )
        commit = output.split()[0]
        if not _SHORT_COMMIT_RE.match(commit):
            raise Exception(f"Invalid commit SHA from ls-remote: {commit}")
        return commit
    except subprocess.TimeoutExpired:
        raise Exception(
            f"git ls-remote timed out after {Config.git_install_timeout}s"
        )


def _resolve_latest_tag(
    git_url: str, tag_pattern: Optional[str] = None, logger: Any = None
) -> Tuple[str, str]:
    """Discover the latest tag matching ``tag_pattern`` and return (tag, commit)."""
    log = logger or Config.logger

    cmd = ["git", "ls-remote", "--tags", "--refs", _authed_git_url(git_url)]

    env = _git_env()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=Config.git_install_timeout,
            env=env,
        )
        if result.returncode != 0:
            raise Exception(
                f"git ls-remote --tags failed: {result.stderr.strip()}"
            )

        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        tags = []
        pattern_re = re.compile(tag_pattern) if tag_pattern else None
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            commit = parts[0]
            ref = parts[1]
            if not ref.startswith("refs/tags/"):
                continue
            tag = ref[len("refs/tags/"):]
            if pattern_re and not pattern_re.search(tag):
                continue
            tags.append((tag, commit))

        if not tags:
            raise Exception(
                f"No tags found"
                + (f" matching pattern '{tag_pattern}'" if tag_pattern else "")
            )

        # Sort by tag name descending (latest convention)
        tags.sort(key=lambda t: t[0], reverse=True)
        return tags[0]
    except subprocess.TimeoutExpired:
        raise Exception(
            f"git ls-remote --tags timed out after {Config.git_install_timeout}s"
        )


# ---------------------------------------------------------------------------
# pip install
# ---------------------------------------------------------------------------


def _install_to_target(
    pip_direct_url: str,
    install_target: str,
    logger: Any = None,
) -> None:
    """Run ``pip install --target <install_target> --no-deps <pip_direct_url>``.

    ``--no-deps`` matches the daemon's dependency policy: MCP module
    packages must depend only on packages already available in the daemon
    environment (or vendor their deps). Module packages declare private
    SilvaEngine libs (e.g. ``silvaengine-utility``) that exist in the
    daemon venv but not on PyPI, so dependency resolution would fail.
    """
    log = logger or Config.logger

    if not _is_safe_path(install_target, Config.git_install_path):
        raise Exception(
            f"Install target '{install_target}' is outside "
            f"git_install_path '{Config.git_install_path}'"
        )

    os.makedirs(install_target, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        install_target,
        "--no-warn-script-location",
        "--no-deps",
        pip_direct_url,
    ]

    log.info(f"Running pip install into {install_target} ...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=Config.git_install_timeout,
            env=_git_env(),
        )
        if result.returncode != 0:
            raise Exception(
                f"pip install failed (rc={result.returncode}):\n{result.stderr.strip()}"
            )
        log.info(f"pip install succeeded for {install_target}")
    except subprocess.TimeoutExpired:
        raise Exception(
            f"pip install timed out after {Config.git_install_timeout}s"
        )


# ---------------------------------------------------------------------------
# Distribution version reading
# ---------------------------------------------------------------------------


def _read_distribution_version(
    install_target: str,
    distribution_name: Optional[str] = None,
) -> Optional[str]:
    """Read the installed package version via importlib.metadata."""
    if not distribution_name:
        return None
    try:
        # Add install_target to path so importlib.metadata can find the dist-info
        original_path = sys.path[:]
        sys.path.insert(0, install_target)
        try:
            from importlib.metadata import version

            return version(distribution_name)
        finally:
            sys.path[:] = original_path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------


def _requirement_satisfied(requirement: str) -> bool:
    """Return True when ``requirement`` is importable/satisfiable in the
    daemon environment (any already-installed version counts)."""
    from importlib.metadata import distribution

    # Strip environment markers and extras: "requests[security]>=2.0 ; python_version<'3.10'"
    req = requirement.split(";")[0].strip()
    if not req:
        return True
    # Strip extras and version specifiers: keep just the name
    name_part = re.split(r"[\[<>=!~;]", req)[0].strip()
    if not name_part:
        return True
    canonical = re.sub(r"[-_.]+", "-", name_part).lower()
    try:
        from importlib.metadata import distributions

        for dist in distributions():
            dist_name = re.sub(r"[-_.]+", "-", (dist.metadata["Name"] or "")).lower()
            if dist_name == canonical:
                return True
    except Exception:
        pass
    # Fallback: module importability (handles vendored / name-mismatch cases)
    import_name = canonical.replace("-", "_")
    try:
        importlib.import_module(import_name)
        return True
    except Exception:
        return False


def _install_missing_dependencies(
    install_target: str,
    logger: Any = None,
) -> Dict[str, Any]:
    """Install the package's dependencies that are missing from the daemon
    environment into the install target.

    Scans ``*.dist-info/METADATA`` ``Requires-Dist`` entries of everything
    freshly installed under ``install_target``. Dependencies already
    satisfied by the daemon environment (e.g. private SilvaEngine libs such
    as ``silvaengine-utility``) are skipped. Missing ones are installed into
    the same ``install_target`` so the module's dependency tree is
    self-contained and importable at runtime.

    Returns ``{"installed": [...], "skipped": [...]}``.
    """
    log = logger or Config.logger

    # Collect Requires-Dist entries from all dist-info dirs under the target
    required: list = []
    dist_info_dirs = []
    for root, dirs, files in os.walk(install_target):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for d in dirs:
            if d.endswith(".dist-info"):
                dist_info_dirs.append(os.path.join(root, d))

    for di in dist_info_dirs:
        metadata_path = os.path.join(di, "METADATA")
        if not os.path.isfile(metadata_path):
            continue
        with open(metadata_path, "r", encoding="utf-8") as f:
            in_requires = False
            for line in f:
                if line.startswith("Requires-Dist:"):
                    in_requires = True
                    req = line[len("Requires-Dist:"):].strip()
                    if req:
                        required.append(req)
                elif in_requires and line[:1] in (" ", "\t") and required:
                    # Continuation line
                    required.append(line.strip())
                else:
                    in_requires = False

    if not required:
        return {"installed": [], "skipped": []}

    missing = [
        req
        for req in required
        if not _requirement_satisfied(req)
    ]

    if not missing:
        log.info(
            f"All {len(required)} dependencies already satisfied by the "
            f"daemon environment; nothing to install."
        )
        return {"installed": [], "skipped": required}

    log.info(
        f"{len(missing)} of {len(required)} dependencies missing from the "
        f"daemon environment; installing into {install_target} ..."
    )

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        install_target,
        "--no-warn-script-location",
        "--upgrade",
        *missing,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=Config.git_install_timeout,
            env=_git_env(),
        )
        if result.returncode != 0:
            raise Exception(
                f"dependency install failed (rc={result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        log.info(f"Dependency install succeeded: {missing}")
        return {"installed": missing, "skipped": required}
    except subprocess.TimeoutExpired:
        raise Exception(
            f"dependency install timed out after {Config.git_install_timeout}s"
        )


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_manifest_from_install(
    install_target: str,
    module_name: str,
    logger: Any = None,
) -> Dict[str, Any]:
    """Load the MCP configuration manifest from an installed Git target.

    Prefers ``mcp_configuration.json`` at the install target root; falls back
    to importing ``module.MCP_CONFIGURATION`` from the install target.
    """
    log = logger or Config.logger

    manifest_path = os.path.join(install_target, "mcp_configuration.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            mcp_configuration = json.load(f)
        log.info(f"Loaded manifest from {manifest_path}")
        return mcp_configuration

    # Fallback: import module.MCP_CONFIGURATION from the install target
    mcp_configuration = _import_mcp_configuration_from_dir(
        install_target, module_name, log
    )
    log.info(
        f"Loaded manifest from module.MCP_CONFIGURATION in {install_target}"
    )
    return mcp_configuration


# ---------------------------------------------------------------------------
# Local / remote install state
# ---------------------------------------------------------------------------


def _get_local_install_state(
    module_setting: Dict[str, Any],
) -> Dict[str, Any]:
    """Read persisted Git install metadata from a module setting dict."""
    if not module_setting:
        return {}
    return {k: module_setting.get(k) for k in _GIT_METADATA_KEYS}


def _needs_refresh(
    local_state: Dict[str, Any],
    remote_commit: str,
    force_refresh: bool = False,
    expected_target: Optional[str] = None,
) -> bool:
    """Decide whether a reinstall is needed based on local vs remote state.

    ``expected_target``: when given, a local ``install_target`` that differs
    from it (e.g. after a GIT_INSTALL_PATH change) also triggers a reinstall
    so the recorded path is brought in line with current configuration.
    """
    if force_refresh:
        return True

    install_target = local_state.get("install_target")
    if not install_target or not os.path.isdir(install_target):
        return True

    if expected_target and install_target != expected_target:
        return True

    resolved_commit = local_state.get("resolved_commit")
    if not resolved_commit:
        return True

    return resolved_commit != remote_commit


def _check_ttl_fresh(
    local_state: Dict[str, Any],
    force_check: bool = False,
) -> bool:
    """Return True if the last check is within TTL (skip remote check)."""
    if force_check:
        return False
    last_checked_at = local_state.get("last_checked_at")
    if not last_checked_at:
        return False
    try:
        last = pendulum.parse(last_checked_at)
        elapsed = (pendulum.now("UTC") - last).total_seconds()
        return elapsed < Config.git_refresh_ttl
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Install metadata helpers
# ---------------------------------------------------------------------------


def _build_git_metadata_defaults() -> Dict[str, Any]:
    """Return default Git metadata keys to inject into module settings."""
    return {k: "" for k in _GIT_METADATA_KEYS}


def _build_install_metadata(
    git_url: str,
    git_ref: str,
    git_subdirectory: str,
    resolved_commit: str,
    install_target: str,
    version_strategy: str,
    distribution_name: Optional[str],
    installed_package_version: Optional[str],
    latest_remote_version: Optional[str],
) -> Dict[str, Any]:
    """Build the Git install metadata dict for persistence in module settings."""
    now = pendulum.now("UTC").isoformat()
    return {
        "git_url": git_url,
        "git_ref": git_ref,
        "git_subdirectory": git_subdirectory or "",
        "install_target": install_target,
        "install_mode": "pip",
        "version_strategy": version_strategy,
        "distribution_name": distribution_name or "",
        "installed_package_version": installed_package_version or "",
        "latest_remote_version": latest_remote_version or "",
        "resolved_commit": resolved_commit,
        "last_checked_commit": resolved_commit,
        "last_checked_at": now,
        "installed_at": now,
    }


def _inject_git_metadata_into_manifest(
    mcp_configuration: Dict[str, Any],
    git_metadata: Dict[str, Any],
) -> None:
    """Inject Git metadata defaults into each module's setting in the manifest.

    Existing setting keys are preserved; Git metadata keys are merged on top.
    """
    modules = mcp_configuration.get("modules", [])
    defaults = _build_git_metadata_defaults()
    for module in modules:
        if not isinstance(module, dict):
            continue
        existing = module.get("setting", {})
        merged = {**defaults, **(existing if isinstance(existing, dict) else {})}
        merged.update(git_metadata)
        module["setting"] = merged


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _clear_install_target(install_target: str, logger: Any = None) -> None:
    """Safely remove an install target directory under git_install_path."""
    log = logger or Config.logger
    if not install_target:
        return
    if not _is_safe_path(install_target, Config.git_install_path):
        log.warning(
            f"Refusing to clear path outside git_install_path: {install_target}"
        )
        return
    try:
        shutil.rmtree(install_target, ignore_errors=True)
        log.info(f"Cleared install target: {install_target}")
    except Exception as e:
        log.warning(f"Failed to clear {install_target}: {e}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def install_mcp_package_from_git(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Install (or refresh) an MCP module from a Git repository.

    Returns a dict with: ``action``, ``resolved_commit``,
    ``installed_package_version``, ``stats``, ``message``.
    """
    logger = info.context.get("logger") or Config.logger
    partition_key = info.context.get("partition_key")

    git_url = kwargs["git_url"]
    git_ref = kwargs.get("git_ref")
    git_subdirectory = kwargs.get("git_subdirectory") or ""
    version_strategy = kwargs.get("version_strategy") or Config.git_version_strategy
    distribution_name = kwargs.get("distribution_name")
    force_refresh = bool(kwargs.get("force_refresh", False))
    module_name = kwargs["module_name"]
    package_name = kwargs["package_name"]
    variables = kwargs.get("variables")
    updated_by = kwargs["updated_by"]

    # --- Validate inputs ---
    _validate_package_name(package_name)
    _validate_package_name(module_name)
    git_url = _validate_git_url(git_url)

    if Config.git_require_ref and not git_ref:
        raise Exception(
            "gitRef is required (git_require_ref is enabled). "
            "Provide a branch, tag, or commit SHA."
        )

    # --- Resolve remote commit ---
    latest_remote_version = None
    if version_strategy == "latest_tag":
        tag, remote_commit = _resolve_latest_tag(
            git_url, Config.git_tag_pattern, logger
        )
        git_ref = tag
        latest_remote_version = tag
    else:
        remote_commit = _resolve_remote_commit(git_url, git_ref, logger)

    # --- Check local state (for refresh / no-op decision) ---
    # Read the existing module setting to get local install metadata.
    local_state = _read_existing_module_setting(
        partition_key, module_name, logger
    )

    # The install target this run would produce. If the recorded target
    # differs (e.g. GIT_INSTALL_PATH changed), refresh to relocate.
    expected_target = _install_target_path(package_name, remote_commit)

    action = "installed"

    if not force_refresh and local_state:
        if not _needs_refresh(
            local_state,
            remote_commit,
            force_refresh=False,
            expected_target=expected_target,
        ):
            # No-op: local install is current.
            logger.info(
                f"Git module '{module_name}' is up to date "
                f"(commit={remote_commit[:12]}). No-op."
            )
            return {
                "action": "noop",
                "resolved_commit": remote_commit,
                "installed_package_version": local_state.get(
                    "installed_package_version", ""
                ),
                "stats": {
                    "tools": 0,
                    "resources": 0,
                    "prompts": 0,
                    "modules": 0,
                    "settings": 0,
                },
                "message": f"Module '{module_name}' is already up to date "
                f"(commit {remote_commit[:12]}).",
            }
        action = "refreshed"

    # --- Install ---
    install_target = expected_target

    # If refreshing, clear old target if different
    old_target = local_state.get("install_target") if local_state else None
    if old_target and old_target != install_target:
        logger.info(
            f"Old install target {old_target} will be cleaned up after "
            f"successful install of {install_target}"
        )

    pip_direct_url = _build_pip_direct_url(
        git_url, git_ref, git_subdirectory
    )

    _install_to_target(pip_direct_url, install_target, logger)

    # --- Install missing dependencies into the target ---
    # Dependencies already satisfied by the daemon environment are skipped;
    # only what the environment lacks is installed into the target dir.
    dep_result = _install_missing_dependencies(install_target, logger)
    if dep_result["installed"]:
        logger.info(
            f"Installed {len(dep_result['installed'])} missing dependencies: "
            f"{dep_result['installed']}"
        )

    # --- Read installed package version ---
    installed_pkg_version = _read_distribution_version(
        install_target, distribution_name
    )

    # --- Load and validate manifest ---
    mcp_configuration = _load_manifest_from_install(
        install_target, module_name, logger
    )

    validate_manifest(
        mcp_configuration, logger=logger, module_name=module_name
    )

    # --- Inject Git metadata into manifest module settings ---
    git_metadata = _build_install_metadata(
        git_url=git_url,
        git_ref=git_ref or "",
        git_subdirectory=git_subdirectory,
        resolved_commit=remote_commit,
        install_target=install_target,
        version_strategy=version_strategy,
        distribution_name=distribution_name,
        installed_package_version=installed_pkg_version,
        latest_remote_version=latest_remote_version,
    )

    _inject_git_metadata_into_manifest(mcp_configuration, git_metadata)

    # --- Persist through the existing loader ---
    Config.clear_mcp_configuration_cache(partition_key)

    load_kwargs = {
        "mcp_configuration": mcp_configuration,
        "module_name": module_name,
        "package_name": package_name,
        "source": "git",
        "updated_by": updated_by,
    }
    if variables:
        load_kwargs["variables"] = variables

    stats = load_mcp_configuration_into_models(info, **load_kwargs)

    # --- Warm cache ---
    try:
        Config.fetch_mcp_configuration(partition_key, force_refresh=True)
        logger.info(f"Cache warmed for partition_key: {partition_key}")
    except Exception as e:
        logger.warning(f"Cache warm failed for {partition_key}: {e}")

    # --- Clean up old install target ---
    if old_target and old_target != install_target:
        _clear_install_target(old_target, logger)

    return {
        "action": action,
        "resolved_commit": remote_commit,
        "installed_package_version": installed_pkg_version or "",
        "stats": stats,
        "message": (
            f"Successfully {action} module '{module_name}' from Git "
            f"(commit {remote_commit[:12]}). "
            f"{stats['tools']} tools, {stats['resources']} resources, "
            f"{stats['prompts']} prompts, {stats['modules']} modules, "
            f"{stats['settings']} settings."
        ),
    }


def _coerce_datetime(value: Any) -> Optional[Any]:
    """Coerce a persisted timestamp to a datetime object for the DateTime scalar.

    Graphene's DateTime scalar serializes datetime objects (and rejects
    strings), while setting rows may hold naive datetime strings with a
    space separator (``2026-09-02 20:40:25.721022``). Parse and return a
    datetime, or None when unparseable/empty.
    """
    if not value:
        return None
    if isinstance(value, pendulum.DateTime):
        return value
    if isinstance(value, datetime.datetime):
        return value
    try:
        return pendulum.parse(str(value))
    except Exception:
        return None


def check_mcp_git_package_version(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Check whether a Git module needs refresh without reinstalling.

    Returns a dict with: ``needs_refresh``, ``local_commit``,
    ``remote_commit``, ``installed_package_version``,
    ``latest_remote_version``, ``last_checked_at``, ``message``.
    """
    logger = info.context.get("logger") or Config.logger
    partition_key = info.context.get("partition_key")
    module_name = kwargs["module_name"]
    force_check = bool(kwargs.get("force_check", False))

    local_state = _read_existing_module_setting(
        partition_key, module_name, logger
    )

    if not local_state or not local_state.get("git_url"):
        return {
            "needs_refresh": True,
            "local_commit": "",
            "remote_commit": "",
            "installed_package_version": "",
            "latest_remote_version": "",
            "last_checked_at": "",
            "message": (
                f"Module '{module_name}' has no Git install metadata. "
                f"Run installMcpPackageFromGit first."
            ),
        }

    local_commit = local_state.get("resolved_commit", "")
    installed_pkg_version = local_state.get("installed_package_version", "")
    last_checked_at = _coerce_datetime(local_state.get("last_checked_at"))

    # Skip remote check if TTL is fresh and not forced
    if _check_ttl_fresh(local_state, force_check):
        return {
            "needs_refresh": False,
            "local_commit": local_commit,
            "remote_commit": local_state.get("last_checked_commit", ""),
            "installed_package_version": installed_pkg_version,
            "latest_remote_version": local_state.get(
                "latest_remote_version", ""
            ),
            "last_checked_at": last_checked_at,
            "message": (
                f"Version check skipped (within TTL of "
                f"{Config.git_refresh_ttl}s). Use forceCheck=true to override."
            ),
        }

    git_url = local_state.get("git_url", "")
    git_ref = local_state.get("git_ref", "")
    version_strategy = local_state.get("version_strategy", "ref")

    try:
        if version_strategy == "latest_tag":
            tag, remote_commit = _resolve_latest_tag(
                git_url, Config.git_tag_pattern, logger
            )
            latest_remote = tag
        else:
            remote_commit = _resolve_remote_commit(git_url, git_ref, logger)
            latest_remote = ""
    except Exception as e:
        logger.warning(f"Remote version check failed for '{module_name}': {e}")
        return {
            "needs_refresh": False,
            "local_commit": local_commit,
            "remote_commit": "",
            "installed_package_version": installed_pkg_version,
            "latest_remote_version": local_state.get(
                "latest_remote_version", ""
            ),
            "last_checked_at": last_checked_at,
            "message": (
                f"Remote check failed (Git unreachable): {e}. "
                f"Local installation remains active."
            ),
        }

    needs_refresh = _needs_refresh(local_state, remote_commit, force_refresh=False)

    # Update last_checked_at / last_checked_commit in the setting row
    _update_check_metadata(
        info, partition_key, module_name, remote_commit, logger
    )

    return {
        "needs_refresh": needs_refresh,
        "local_commit": local_commit,
        "remote_commit": remote_commit,
        "installed_package_version": installed_pkg_version,
        "latest_remote_version": latest_remote,
        "last_checked_at": pendulum.now("UTC"),
        "message": (
            f"Refresh needed: local {local_commit[:12]} → "
            f"remote {remote_commit[:12]}"
            if needs_refresh
            else f"Up to date (commit {remote_commit[:12]})"
        ),
    }


def refresh_mcp_git_package(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Refresh a Git-installed module using persisted Git metadata.

    Reads the existing module setting for ``git_url``, ``git_ref``, etc.,
    then calls ``install_mcp_package_from_git`` with ``forceRefresh``.
    """
    logger = info.context.get("logger") or Config.logger
    partition_key = info.context.get("partition_key")
    module_name = kwargs["module_name"]
    force_refresh = bool(kwargs.get("force_refresh", False))
    updated_by = kwargs["updated_by"]

    local_state = _read_existing_module_setting(
        partition_key, module_name, logger
    )

    if not local_state or not local_state.get("git_url"):
        raise Exception(
            f"Module '{module_name}' has no Git install metadata. "
            f"Run installMcpPackageFromGit first."
        )

    install_kwargs = {
        "git_url": local_state["git_url"],
        "git_ref": local_state.get("git_ref"),
        "git_subdirectory": local_state.get("git_subdirectory"),
        "version_strategy": local_state.get("version_strategy", "ref"),
        "distribution_name": local_state.get("distribution_name"),
        "force_refresh": force_refresh,
        "module_name": module_name,
        "package_name": _read_module_package_name(
            partition_key, module_name, logger
        ),
        "updated_by": updated_by,
    }

    # Preserve variables from the original install if any
    return install_mcp_package_from_git(info, **install_kwargs)


# ---------------------------------------------------------------------------
# Internal helpers for reading/writing module setting metadata
# ---------------------------------------------------------------------------


def _read_existing_module_setting(
    partition_key: str,
    module_name: str,
    logger: Any = None,
) -> Dict[str, Any]:
    """Read the module's setting dict from the cached MCP configuration.

    Returns an empty dict if the module is not found or has no setting.
    """
    log = logger or Config.logger
    try:
        config = Config.fetch_mcp_configuration(partition_key)
        for module in config.get("modules", []):
            if module.get("module_name") == module_name:
                setting = module.get("setting", {})
                if isinstance(setting, dict):
                    return setting
                return {}
        return {}
    except Exception as e:
        log.warning(
            f"Failed to read module setting for '{module_name}': {e}"
        )
        return {}


def _read_module_package_name(
    partition_key: str,
    module_name: str,
    logger: Any = None,
) -> str:
    """Read the module's package_name from cached configuration."""
    log = logger or Config.logger
    try:
        config = Config.fetch_mcp_configuration(partition_key)
        for module in config.get("modules", []):
            if module.get("module_name") == module_name:
                return module.get("package_name", module_name)
        return module_name
    except Exception:
        return module_name


def _update_check_metadata(
    info: ResolveInfo,
    partition_key: str,
    module_name: str,
    remote_commit: str,
    logger: Any = None,
) -> None:
    """Update ``last_checked_at`` and ``last_checked_commit`` in the setting row.

    This performs a lightweight GraphQL mutation to update the setting
    with the new check metadata. Errors are logged but not raised — a
    failed metadata update should not block the version check response.
    """
    log = logger or Config.logger
    try:
        local_state = _read_existing_module_setting(
            partition_key, module_name, log
        )
        if not local_state or not local_state.get("setting_id"):
            return

        # Merge new check metadata into the existing setting
        setting_id = local_state["setting_id"]
        updated_setting = dict(local_state)
        updated_setting["last_checked_at"] = pendulum.now("UTC").isoformat()
        updated_setting["last_checked_commit"] = remote_commit

        # Remove setting_id and non-setting keys before update
        setting_payload = {
            k: v
            for k, v in updated_setting.items()
            if k not in ("setting_id",)
        }

        from .config import _dispatch_internal_graphql

        _dispatch_internal_graphql(
            context={"partition_key": partition_key},
            query="""
            mutation updateCheckMetadata(
                $settingId: String!
                $setting: JSONCamelCase
                $updatedBy: String!
            ) {
                insertUpdateMcpSetting(
                    settingId: $settingId
                    setting: $setting
                    updatedBy: $updatedBy
                ) {
                    mcpSetting {
                        settingId
                    }
                }
            }
            """,
            variables={
                "settingId": setting_id,
                "setting": setting_payload,
                "updatedBy": "git_version_check",
            },
        )
    except Exception as e:
        log.warning(
            f"Failed to update check metadata for '{module_name}': {e}"
        )


__all__ = [
    "install_mcp_package_from_git",
    "check_mcp_git_package_version",
    "refresh_mcp_git_package",
]