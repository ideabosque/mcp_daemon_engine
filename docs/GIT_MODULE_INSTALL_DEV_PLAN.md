# Git Module Installation Development Plan

> Status: Phases 1–3 implemented and live-verified (install, refresh, version check, runtime execution against `git@github.com:ideabosque/mcp_hospirfq_processor.git`). Phase 4 (S3 removal) pending.
> Document version: 1.1
> Last updated: 2026-09-02
> Owner: mcp-daemon-engine

## 1. Goal

Replace MCP Python module deployment (ZIP upload plus runtime `sys.path` extraction) with Git-based installation. **The S3 package deployment option is eliminated entirely** — there is no compatibility flag, no legacy `source="s3"` runtime branch, and no ZIP upload API. Git is the only mechanism for deploying MCP module packages.

The target workflow is:

1. An admin provides a Git repository URL, optional ref, module name, and package name.
2. The daemon validates and installs the package from Git into a controlled local install/cache directory.
3. The daemon records the locally installed version state: requested ref, resolved commit, optional package version, install target, and install timestamp.
4. The daemon reads the package manifest from `mcp_configuration.json` or `module.MCP_CONFIGURATION`.
5. The daemon persists tools, resources, prompts, module links, modules, and settings through the existing model loader.
6. Runtime execution imports the installed package through a deterministic import path — no ZIP download, no ad hoc ZIP extraction.
7. A refresh check compares the local version state with Git and reinstalls only when the configured upstream version has changed or the local install is missing/corrupt.

The existing external MCP proxy flow stays separate and continues to use `source="external"`.

Source model:

| Source | Status | Meaning |
| ------ | ------ | ------- |
| `git` | Primary | Python MCP modules installed from Git into local artifact storage. |
| `external` | Keep | Remote MCP servers proxied over HTTP. |
| `local` / empty | Keep | Built-in or environment-installed development modules. |
| `s3` | **Removed** | ZIP package deployment is eliminated. Existing `source="s3"` rows must be migrated to `source="git"` before the removal release (see §12). |

> Note: S3 the **service** is not removed from the daemon — `mcp_function_call` large-content offload (and any other non-package S3 usage) still uses `Config.aws_s3` / `Config.funct_bucket_name`. What is removed is the S3 *package deployment* surface: upload APIs, ZIP download/extract, and the `source="s3"` runtime branch.

## 2. Current State and S3 Removal Scope

The legacy package flow (documented in `docs/MCP_PACKAGE_UPLOAD_SPEC.md`) is being removed. Every S3 package surface is deleted, not gated:

| Area | Legacy behavior | Action |
| ---- | --------------- | ------ |
| Upload API | `generateMcpPackageUploadUrl` creates a presigned S3 PUT URL for `{packageName}.zip`. | **Remove** the mutation from `schema.py`, `mutations/mcp_upload.py`, and `deploy()`. |
| Process API | `processMcpPackage` downloads the ZIP from S3, validates, extracts, persists, warms cache. | **Remove** the mutation and the `process_mcp_package()` handler. |
| Base64 shortcut | `loadMcpConfiguration(packageBase64: ...)` decodes a ZIP, uploads to S3, processes. | **Remove** the `packageBase64` argument and `process_base64_package()`. |
| Runtime package source | `source="s3"` triggers ZIP download/extract. | **Remove** the `s3` branch from `_get_module()`; raise a clear migration-required error if a row still carries `source="s3"`. |
| Runtime import path | `_download_and_extract_package()` / `_import_s3_module()` / `_import_module_from_extract_path()`. | **Remove** from `mcp_utility.py` and `mcp_handlers.py`. |
| Feature flag | `ENABLE_S3_PACKAGE_UPLOAD` gated the upload APIs. | **Remove** the flag from `Config`, `settings.yaml`, and all env examples — there is nothing left to gate. |
| `FUNCT_BUCKET_NAME` for modules | Required for package staging. | **No longer required for module deployment.** Retained only for `mcp_function_call` content offload. |

What is kept and reused:

| Area | Behavior |
| ---- | ---------------- |
| Manifest loading | Package manifests prefer `mcp_configuration.json` and fall back to importing `module.MCP_CONFIGURATION` (implemented in the Git installer). |
| Persistence | `load_mcp_configuration_into_models()` accepts an explicit manifest dict and persists it without importing the module — the Git installer's persistence sink. |
| Manifest validation | `validate_manifest()` is reused by the Git installer before persistence. |

## 3. Constraints and Existing Extension Points

- `MCPModule` currently stores only `module_name`, `package_name`, `classes`, `source`, and audit timestamps in both DynamoDB and PostgreSQL.
- `MCPSetting.setting` is flexible JSON and can store installation metadata without a table migration.
- `MCPFunction.data` is flexible JSON and already stores extra metadata such as `external_name`.
- The dispatcher has a source switch in `_get_module()`. This is a provider switch for `external`, `git`, and local direct imports only — no `s3` branch after the removal phase.
- `load_mcp_configuration_into_models()` is the right persistence sink and is reused.
- The codebase has no committed test suite convention beyond `pyproject.toml` pytest settings and `ruff`; add focused tests if introducing a test runner is acceptable.

## 4. Proposed Design

Add a Git package source with `MCPModule.source == "git"`.

Git install metadata is stored in the module setting row:

```json
{
  "git_url": "https://github.com/org/repo.git",
  "git_ref": "v1.2.3",
  "git_subdirectory": "",
  "install_target": "/tmp/packages/<packageName>/<resolvedCommit>",
  "install_mode": "pip",
  "version_strategy": "ref",
  "distribution_name": "",
  "installed_package_version": "1.2.3",
  "latest_remote_version": "1.2.4",
  "resolved_commit": "<commit-sha>",
  "last_checked_commit": "<commit-sha>",
  "last_checked_at": "<utc timestamp>",
  "installed_at": "<utc timestamp>"
}
```

This avoids a schema migration. If the system later needs list/filter queries by repository URL, ref, or commit, add explicit columns to `MCPModule` or a separate module deployment table.

The installer is `pip install --target <install_target> --no-deps git+<url>@<ref>` because it honors normal Python packaging metadata while keeping the module isolated from the daemon environment. `--no-deps` is deliberate: module packages declare private SilvaEngine libraries (e.g. `silvaengine-utility`) that exist in the daemon environment but not on PyPI, so dependency resolution would fail. Missing dependencies are handled explicitly (see below). For repositories that contain the package in a subdirectory, support the pip direct URL fragment:

```text
git+https://github.com/org/repo.git@ref#subdirectory=path/to/package
```

The daemon does not clone arbitrary code into the current working tree, and it does not install packages into the daemon's own environment.

Git package artifacts live under a single root:

```text
Git installations: Config.git_install_path, default /tmp/packages
```

Git install targets are commit-scoped:

```text
{Config.git_install_path}/{packageName}/{resolvedCommit}/
```

This gives Git packages a dedicated lifecycle, dependency tree, and cleanup boundary, fully separate from any other artifact storage the daemon uses.

### Dependency policy

Module packages must depend only on packages already available in the daemon environment or on installable third-party packages. After the module install, the handler scans `*.dist-info/METADATA` `Requires-Dist` entries of everything freshly installed under the target:

1. Requirements already satisfied by the daemon environment (checked via `importlib.metadata`, PEP 503 name normalization, extras and environment markers stripped) are **skipped** — they are not installed again.
2. Missing requirements are installed **into the install target** (`pip install --target <install_target> <reqs>`), so the module's dependency tree is self-contained and importable at runtime.

## 5. Version and Refresh Model

The Git provider needs separate concepts:

| Concept | Meaning |
| ------- | ------- |
| Requested version | What the admin asked to track, such as `gitRef=v1.2.3`, `gitRef=main`, or a semantic version tag. |
| Installed version | What is currently installed locally, represented by `resolved_commit`, optional package metadata version, and `installed_at`. |
| Remote version | What Git currently resolves for the requested version, represented by `remote_commit` and optional latest tag/package version. |

Commit SHA comparison is the authoritative refresh decision.

Decision rules:

1. If `install_target` is missing, reinstall.
2. If `install_target` exists but differs from the currently expected target (e.g. `GIT_INSTALL_PATH` configuration changed), reinstall into the expected target and clean up the old one.
3. If `resolved_commit` is missing, reinstall and persist the resolved commit.
4. If `git_ref` is an immutable commit SHA and it equals `resolved_commit`, do not reinstall.
5. If `git_ref` is a branch or tag, run `git ls-remote <url> <ref>` and compare the returned commit with `resolved_commit`.
6. If the remote commit differs from `resolved_commit`, reinstall into a new target directory, validate the manifest, persist metadata, purge import cache, and warm MCP configuration cache.
7. If the remote commit is unchanged, return a no-op result and update only `last_checked_at` / `last_checked_commit`.
8. If Git cannot be reached, keep the current local installation and report the check failure. Do not delete a working local install before a replacement has been successfully installed and validated.

The install target is derived from the resolved commit, not the moving ref:

```text
{git_install_path}/{packageName}/{resolvedCommit}/
```

That allows two versions to coexist during refresh. Once the new version is installed, validated, and persisted, older commit directories for the same package can be removed if no active module setting references them.

Semantic version support layered on top of commit comparison:

- `version_strategy="ref"`: track the explicitly supplied `gitRef`. This is the default and safest behavior.
- `version_strategy="latest_tag"`: discover the latest allowed tag from Git, resolve that tag to a commit, then compare with `resolved_commit`.
- `version_strategy="package_version"`: after install, read the installed package version from `importlib.metadata.version(distribution_name)` when a distribution name is supplied.

`ref` and `latest_tag` are implemented; `package_version` is read for information when `distributionName` is supplied but is not used as a refresh trigger, because Python import module names and package distribution names can differ.

## 6. Configuration

Settings/environment variables for installation control:

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `git_install_path` / `GIT_INSTALL_PATH` | `/tmp/packages` | Parent directory for installed Git packages. In Docker, point at the bind-mounted data volume (e.g. `/app/data/packages`) so installs survive container restarts. Also hosts the temporary SSH key file when `GIT_SSH_KEY` is set. |
| `git_install_timeout` / `GIT_INSTALL_TIMEOUT` | `300` | Max seconds for one install operation. |
| `git_allowed_hosts` / `GIT_ALLOWED_HOSTS` | `github.com` | Comma-separated allowlist for Git hosts. |
| `git_token` / `GIT_TOKEN` | unset | Optional token for private HTTPS repositories. Injected into `git ls-remote` and the pip direct URL as `x-access-token`. Never logged, never persisted. Ignored for SSH URLs. |
| `git_ssh_key` / `GIT_SSH_KEY` | unset | Optional private SSH key material for `git+ssh` URLs (single-line PEM with `\n` escapes). Written to a `0600` file under `git_install_path` and passed to git via `GIT_SSH_COMMAND`. Leave unset to use the system SSH setup (`~/.ssh`, ssh-agent, or a baked-in deploy key). Ignored for HTTPS URLs. |
| `git_require_ref` / `GIT_REQUIRE_REF` | `true` | Require branch, tag, or commit instead of accepting moving default branch. |
| `git_refresh_policy` / `GIT_REFRESH_POLICY` | `manual` | `manual`, `on_startup`, or `on_runtime_miss`. |
| `git_refresh_ttl` / `GIT_REFRESH_TTL` | `3600` | Minimum seconds between remote version checks for the same module. |
| `git_version_strategy` / `GIT_VERSION_STRATEGY` | `ref` | Default version discovery strategy for Git modules. |
| `git_tag_pattern` / `GIT_TAG_PATTERN` | unset | Optional regex for tags when `version_strategy="latest_tag"`. |

`Config._set_parameters()` reads these values, and `Config.initialize()` creates the install directory. No S3 package-upload flag exists — there is no S3 package surface to toggle.

## 7. GraphQL API

Git installation is a new mutation; the legacy upload mutations are removed entirely (see §12).

```graphql
type InstallMcpPackageFromGitPayload {
    ok: Boolean!
    message: String
    stats: McpConfigurationStats
    resolvedCommit: String
    installedPackageVersion: String
    action: String
}

extend type Mutation {
    installMcpPackageFromGit(
        gitUrl: String!
        gitRef: String
        gitSubdirectory: String
        versionStrategy: String
        distributionName: String
        forceRefresh: Boolean
        moduleName: String!
        packageName: String!
        variables: JSONCamelCase
        updatedBy: String!
    ): InstallMcpPackageFromGitPayload
}
```

Behavior:

1. Validate `packageName` and `moduleName` using the existing name policy.
2. Validate `gitUrl` scheme and host against the allowlist (HTTPS and SSH forms accepted).
3. Require `gitRef` when `git_require_ref` is enabled.
4. Resolve the requested ref or discovered version to a commit SHA before persistence.
5. Compare the remote commit with the locally persisted `resolved_commit` unless `forceRefresh=true`; also reinstall when the persisted `install_target` differs from the currently expected target.
6. Return `action="noop"` if the local installation exists and the remote commit has not changed.
7. Install the package into a deterministic commit-scoped target directory.
8. Install missing dependencies into the target (skip what the daemon environment already satisfies).
9. Load and validate the manifest.
10. Persist through `load_mcp_configuration_into_models(..., source="git")`.
11. Store Git install/version metadata in the setting row by predeclaring metadata keys in the manifest's module `setting`, then passing concrete values via `variables`.
12. Clear and warm `Config.mcp_configuration` for the active partition key.
13. Return `action="installed"`, `action="refreshed"`, or `action="noop"`.

A second mutation performs version checks without a full install request:

```graphql
type CheckMcpGitPackageVersionPayload {
    ok: Boolean!
    message: String
    needsRefresh: Boolean
    localCommit: String
    remoteCommit: String
    installedPackageVersion: String
    latestRemoteVersion: String
    lastCheckedAt: DateTime
}

extend type Mutation {
    checkMcpGitPackageVersion(
        moduleName: String!
        forceCheck: Boolean
    ): CheckMcpGitPackageVersionPayload
}
```

`checkMcpGitPackageVersion` reads the module's setting metadata from the cached configuration, performs a remote check when TTL allows (or `forceCheck=true`), updates `last_checked_at` metadata, and reports whether refresh is needed. It never reinstalls.

A third mutation provides explicit refresh by module:

```graphql
extend type Mutation {
    refreshMcpGitPackage(
        moduleName: String!
        forceRefresh: Boolean
        updatedBy: String!
    ): InstallMcpPackageFromGitPayload
}
```

`refreshMcpGitPackage` reuses persisted Git metadata and follows the same no-op/install decision rules as `installMcpPackageFromGit`.

Register the mutations in:

- `mcp_daemon_engine/mutations/mcp_git.py`
- `mcp_daemon_engine/schema.py`
- `mcp_daemon_engine/main.py` deployment metadata

The legacy S3 mutations (`generateMcpPackageUploadUrl`, `processMcpPackage`) and the Base64 ZIP argument are **removed** from `schema.py` and `main.py` in the S3 removal phase — not flagged, not retained.

## 8. Installer Handler

Implemented in `mcp_daemon_engine/handlers/mcp_git.py`.

Public entry points:

```python
def install_mcp_package_from_git(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Dict[str, Any]: ...
def check_mcp_git_package_version(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Dict[str, Any]: ...
def refresh_mcp_git_package(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Dict[str, Any]: ...
```

Helper responsibilities (as implemented):

| Helper | Responsibility |
| ------ | -------------- |
| `_validate_git_url()` | Accept HTTPS and SSH URL forms (`https://...`, `ssh://...`, `git@host:path`); enforce the host allowlist; reject `file://`, local paths, and other schemes. |
| `_normalize_scp_url()` | Convert scp-style `git@host:path` to `ssh://git@host/path` for pip (plain git accepts both; pip requires the `ssh://` form). |
| `_authed_git_url()` | Inject `GIT_TOKEN` into HTTPS URLs (ls-remote and pip alike); return SSH URLs unchanged. |
| `_build_pip_direct_url()` | Convert URL/ref/subdirectory into a pip-compatible direct URL with token injection and scp normalization applied. |
| `_git_env()` / `_git_ssh_command()` | Build the subprocess environment; write `GIT_SSH_KEY` material to a `0600` file and expose it via `GIT_SSH_COMMAND` when set, otherwise inherit the system SSH setup. |
| `_install_to_target()` | Run `pip install --target ... --no-deps` with timeout into the commit-scoped target directory. |
| `_install_missing_dependencies()` | Scan `Requires-Dist` of installed dist-info metadata; skip requirements the daemon environment satisfies; install the rest into the target. |
| `_resolve_remote_commit()` | Resolve ref to immutable commit SHA via `git ls-remote` (short-circuit 40-char SHAs). |
| `_resolve_latest_tag()` | Discover the latest tag matching `git_tag_pattern` via `git ls-remote --tags`. |
| `_get_local_install_state()` / `_read_existing_module_setting()` | Read persisted setting metadata from the cached MCP configuration. |
| `_needs_refresh()` | Compare local and remote state, honoring `forceRefresh`, missing targets, and install-target drift. |
| `_check_ttl_fresh()` | Skip remote checks within `git_refresh_ttl` unless forced. |
| `_read_distribution_version()` | Read installed distribution version via `importlib.metadata` when `distributionName` is configured. |
| `_load_manifest_from_install()` | Prefer `mcp_configuration.json`; fall back to importing `module.MCP_CONFIGURATION` from the install target. |
| `_inject_git_metadata_into_manifest()` | Merge Git metadata defaults + concrete values into each manifest module's `setting`. |
| `_clear_install_target()` | Safely remove only paths under `git_install_path`. |
| `_update_check_metadata()` | Persist `last_checked_at` / `last_checked_commit` after a version check (best-effort). |

Use `subprocess.run()` with argument lists, not shell strings, for `pip` and `git` calls.

Refresh safety rule: install into a commit-scoped target first. Only update persisted metadata and import cache after installation and manifest validation succeed. Keep the previous target usable until the new target is ready, then clean it up.

## 9. Runtime Loader Changes

`_get_module()` in `handlers/mcp_utility.py` uses explicit provider branches:

```python
source_key = (source or "local").lower()

if source_key == "external":
    ...
elif source_key == "git":
    return _import_git_module(module_name, module_setting)
elif source_key == "local":
    return importlib.import_module(module_name)
else:
    raise Exception(f"Unsupported MCP module source: {source}")
```

After the S3 removal phase there is no `s3` branch: any row still carrying `source="s3"` raises a clear migration-required error instead of silently downloading a ZIP. Both `None` and `""` mean `local` direct import, replacing the ambiguous legacy behavior where `source == ""` fell through to the extracted-package path.

The runtime module dict contains `setting`; `_get_class()` passes `module_setting` through to `_get_module()` so Git modules resolve their `install_target`.

For `source="git"`:

1. Read `install_target` from the module setting.
2. If missing entirely, fail with "Run installMcpPackageFromGit first".
3. If the directory is missing, fail with a clear reinstall instruction (auto-reinstall from the runtime path requires a ResolveInfo context the runtime does not have; `git_refresh_policy=on_runtime_miss` logs a warning and the error directs the admin to `refreshMcpGitPackage`).
4. Insert `install_target` into `sys.path` and import using the same cache-purge approach as the legacy extractor.

Runtime does not check Git on every tool call. Policy:

- `git_refresh_policy="manual"` (default): runtime imports the persisted local installation and only errors when the target is missing.
- `git_refresh_policy="on_runtime_miss"`: logs a warning on missing targets (auto-reinstall from runtime is deferred; see Open Decisions).
- `git_refresh_policy="on_startup"`: a startup hook checks all Git modules subject to `git_refresh_ttl`; changed modules are refreshed before serving traffic when feasible.

Avoid `on_every_call`; remote checks add latency, create Git rate-limit risk, and make tool execution depend on network availability.

Git packages are never installed into any other module artifact directory — `/tmp/packages` (or the configured `git_install_path`) is the only Git import root.

## 10. Manifest Contract

Git packages support the same manifest shape the ZIP flow used:

```text
repo/
|-- pyproject.toml
|-- mcp_configuration.json
|-- package_module/
    |-- __init__.py
```

The manifest declares module settings keys that may be overridden by deployment variables. To store Git metadata through the existing loader, the handler injects metadata defaults into each module before validation/persistence:

```python
module["setting"] = {
    **module.get("setting", {}),
    "git_url": "",
    "git_ref": "",
    "git_subdirectory": "",
    "install_target": "",
    "install_mode": "pip",
    "version_strategy": "ref",
    "distribution_name": "",
    "installed_package_version": "",
    "latest_remote_version": "",
    "resolved_commit": "",
    "last_checked_commit": "",
    "last_checked_at": "",
    "installed_at": "",
}
```

Then the actual values are merged in before persistence, so the loader's existing override logic fills them.

## 11. Security Requirements

- HTTPS is the default Git URL scheme. SSH is explicitly supported via the system SSH setup (`~/.ssh`, ssh-agent, baked-in deploy keys) or `GIT_SSH_KEY`; both schemes pass through the host allowlist.
- Require a ref by default. Moving branches such as `main` should be allowed only when explicitly configured (`GIT_REQUIRE_REF=false`).
- Resolve and persist the commit SHA used for installation.
- For branch or latest-tag tracking, compare remote and local commits before refresh; do not trust a semantic version string alone.
- Never log tokens or SSH keys; never embed `GIT_TOKEN` or `GIT_SSH_KEY` in persisted settings.
- Restrict install paths to the configured parent directory before deleting or replacing them.
- Reject local paths, `file://` URLs, and unsupported hosts.
- Prefer `pip --target` over global environment installs; module installs use `--no-deps` with an explicit missing-dependency step.
- Consider adding a future `git_allowed_repositories` allowlist for production.

## 12. S3 Removal

S3 package deployment is eliminated outright. The removal checklist:

1. Inventory all active `MCPModule` rows where `source="s3"` or `source` is truthy and not `git`/`external`.
2. Build an operator-provided mapping from each legacy `packageName`/`moduleName` to `gitUrl`, `gitRef`, optional `gitSubdirectory`, optional `distributionName`, and version strategy.
3. For each row, run `installMcpPackageFromGit` (the `deploy_mcp_git.py` gateway script automates this).
4. Verify each persisted module row is `source="git"` with `resolved_commit` and `install_target`, and execute at least one tool/resource/prompt per migrated module.
5. Remove the `generateMcpPackageUploadUrl` and `processMcpPackage` mutations from `schema.py`, delete `mutations/mcp_upload.py`, and remove the actions from `deploy()` in `main.py`.
6. Remove the `packageBase64` argument and `process_base64_package()` from `loadMcpConfiguration` / `mcp_handlers.py`.
7. Remove the `s3` branch and `_import_s3_module()` / `_download_and_extract_package()` / `_import_module_from_extract_path()` from `mcp_utility.py`; the `s3` source becomes an unsupported-source error.
8. Remove `enable_s3_package_upload` from `Config`, the gateway `settings.yaml`, and all env example files.
9. Keep `Config.aws_s3` and `FUNCT_BUCKET_NAME` initialization — `mcp_function_call` content offload still requires them — but they are no longer part of module deployment.
10. Mark `docs/MCP_PACKAGE_UPLOAD_SPEC.md` as retired.

Hard removal is acceptable only when no production rows still require `source="s3"`, or every affected module has a known Git repository/ref and is migrated in the same release. Do not reinterpret old `source="s3"` rows as Git rows — migration is explicit, per module, via the install mutation.

## 13. Implementation Phases

### Phase 1: Foundations — Complete

- Git install settings in `Config` (including `git_ssh_key`; `git_clone_path` was dropped during implementation as unused — the SSH key file lives under `git_install_path`).
- `mcp_git.py` handler: URL validation (HTTPS + SSH), scp normalization, token injection, commit-scoped target generation, `--no-deps` install, dependency scan/install, manifest loading with import fallback.
- Local/remote version state, commit comparison, TTL handling.
- Unit-level tests for URL validation and target path safety still to add.

### Phase 2: Install Mutations — Complete

- `InstallMcpPackageFromGit`, `CheckMcpGitPackageVersion`, `RefreshMcpGitPackage` registered in `schema.py` and `main.py`.
- Reuses `validate_manifest()` and `load_mcp_configuration_into_models()`.
- Cache clear + warm after successful persistence.
- Payloads return `resolvedCommit`, installed package version, and `action`.
- Interim S3 compatibility flag was added during development; removed in Phase 4.

### Phase 3: Runtime Git Source — Complete, live-verified

- Explicit `source="git"` branch in `_get_module()`; `_get_class()` passes `module_setting` through at all three call sites (tool/resource/prompt).
- Imports from the commit-scoped `install_target`.
- Honors `git_refresh_policy`; no remote checks on every execution.
- `source="external"` and local direct import preserved.
- Live-verified end-to-end: install → dependency scan → version check (TTL + forced) → no-op → runtime `tools/call` from the install target.

### Phase 4: S3 Removal — Pending

- Execute the §12 removal checklist (mutations, Base64 flow, runtime `s3` branch, feature flag, env examples).
- Add a migration-required error for any residual `source="s3"` row encountered at runtime.
- Update `docs/MCP_PACKAGE_UPLOAD_SPEC.md` with retired status.

### Phase 5: Operations and Docs — Pending

- Add docs for package repository layout and install mutation examples.
- Add logging/metrics around install duration, resolved commit, version checks, cache hits, no-op checks, and reinstall attempts.
- Add rollback guidance: Git-installed modules can be re-pinned to the previous commit via `installMcpPackageFromGit` with the prior ref/SHA.

## 14. Acceptance Criteria

Verified:

- `installMcpPackageFromGit` installs a Git package at a pinned tag, commit, or branch (SSH and HTTPS, public and private).
- The mutation reads `mcp_configuration.json` from the installed package and persists the expected rows; falls back to `module.MCP_CONFIGURATION` when no manifest file exists.
- Persisted modules use `source="git"` and include Git install metadata in their settings: requested ref, resolved commit, optional package version, install target, and install/check timestamps.
- Dependencies already present in the daemon environment are skipped; missing ones are installed into the install target.
- A version check reports `needsRefresh=false` when Git resolves the requested ref to the installed commit, and `needsRefresh=true` when it resolves to a different commit.
- Refresh installs a changed version into a new target, validates it, updates metadata, purges import cache, and warms configuration cache.
- Refresh returns a no-op result when the remote commit is unchanged and the local installation exists.
- Runtime tool/resource/prompt execution imports from the commit-scoped `install_target` under `git_install_path`.
- Restarting the daemon can execute already-installed Git modules without reinstalling.
- If Git is unreachable during a version check, the existing local installation remains active.
- `source="external"` proxy modules continue to work unchanged.
- Private repository installs work over SSH (system key or `GIT_SSH_KEY`) and HTTPS (`GIT_TOKEN`) without leaking credentials to logs or database rows.
- Unsupported Git hosts, missing refs when required, invalid package names, and unsafe install paths fail with clear GraphQL error messages.

Pending (Phase 4):

- No S3 package surfaces remain: `generateMcpPackageUploadUrl`, `processMcpPackage`, and `packageBase64` are removed from the schema and `deploy()`.
- Any residual `source="s3"` row fails at runtime with a clear migration-required error.
- `enable_s3_package_upload` no longer exists in `Config` or the gateway settings.

## 15. Test Plan

| Test | Target |
| ---- | ------ |
| Validate Git URL | Accept allowed HTTPS and SSH Git URLs; reject `file://`, local paths, unsupported hosts, and malformed URLs. |
| Require ref | Verify `git_require_ref=true` rejects empty `gitRef`. |
| Build direct URL | Verify ref, subdirectory, scp normalization, and token injection are encoded into the pip direct URL. |
| Install target safety | Verify generated paths stay under `git_install_path`; replacement refuses paths outside it. |
| Resolve remote commit | Mock `git ls-remote`; verify branch/tag refs resolve to commit SHAs; 40-char SHAs short-circuit. |
| No-op version check | Local `resolved_commit` matches remote commit and install target exists; assert no reinstall. |
| Refresh-needed check | Remote commit differs; assert `needsRefresh=true`. |
| Install-target drift | Persisted `install_target` differs from expected (config change); assert reinstall. |
| TTL behavior | Recent `last_checked_at` skips remote check unless `forceCheck=true`. |
| Failed remote check | Git check failure leaves local install active and reports a clear error. |
| Manifest file load | Load `mcp_configuration.json` from a fake installed package. |
| Manifest import fallback | Import `module.MCP_CONFIGURATION` from a fake install target with cache restoration. |
| Dependency scan | Requirements satisfied by the daemon environment are skipped; missing ones are pip-installed into the target. |
| Mutation success | Mock installer and loader; assert `source="git"`, version metadata injection, cache clear, and cache warm. |
| Refresh mutation no-op | Mock unchanged remote commit; assert loader is not called. |
| Refresh mutation changed | Mock changed remote commit; assert new target install, manifest validation, loader call, cache purge, and metadata update. |
| Runtime import | `_get_module(..., source="git")` imports from install target. |
| Runtime reinstall | Missing install target errors with a clear reinstall instruction (auto-reinstall deferred). |
| External compatibility | Existing `source="external"` branch remains covered. |
| S3 surfaces removed | Schema introspection exposes no `generateMcpPackageUploadUrl`/`processMcpPackage`; `packageBase64` is rejected; `_get_module(source="s3")` raises the migration-required error. |

## 16. Open Decisions

- Should production allow moving branch refs, or require immutable tags/commit SHAs only?
- What tag pattern should define eligible release tags for `latest_tag`?
- Should refresh checks happen only through admin GraphQL calls, or should there be startup refresh for selected deployments?
- Should package distribution name be required when reading `installed_package_version`, or should package version be informational only?
- Should private repositories be supported only through `GIT_TOKEN`/SSH keys, or also through SSH deploy keys provisioned per deployment (current: both work; bake-in is the Docker default)?
- Should install metadata stay in `MCPSetting.setting`, or should a future `MCPModuleDeployment` table track repo URL, ref, commit, status, and errors?
- Should runtime auto-reinstall be implemented for `git_refresh_policy="on_runtime_miss"` (currently logs a warning and errors), or should missing packages always fail until an admin re-runs the install mutation?
- When should Phase 4 (S3 removal) ship, and are there production `source="s3"` rows that must be migrated first?

## 17. Reference Map

- `mcp_daemon_engine/handlers/mcp_git.py`: Git installer (URL validation, pip install, commit resolution, dependency scan, manifest loading, version checks).
- `mcp_daemon_engine/mutations/mcp_git.py`: GraphQL mutations for install/check/refresh.
- `mcp_daemon_engine/handlers/mcp_utility.py`: runtime source dispatch and dynamic module import.
- `mcp_daemon_engine/handlers/config.py`: daemon settings, Git install settings, AWS clients, and MCP configuration cache.
- `mcp_daemon_engine/handlers/mcp_handlers.py`: manifest validation and model-loading sink; its ZIP/Base64 helpers are removal targets (Phase 4).
- `mcp_daemon_engine/mutations/mcp_upload.py`: legacy upload mutations — removal target (Phase 4).
- `mcp_daemon_engine/mutations/mcp_configuration.py`: inline configuration entry point; `packageBase64` branch is a removal target (Phase 4).
- `mcp_daemon_engine/models/dynamodb/mcp_module.py`: DynamoDB module persistence with `source`.
- `mcp_daemon_engine/models/postgresql/mcp_module.py`: PostgreSQL module persistence with `source`.
- `mcp_daemon_engine/schema.py`: Graphene mutation registration.
- `mcp_daemon_engine/main.py`: SilvaEngine deployment metadata and GraphQL dispatch entry point.
- `silvaengine_gateway/silvaengine_gateway/tests/deploy_mcp_git.py`: operator deployment script (install/check/refresh via the gateway).
- `docs/MCP_PACKAGE_UPLOAD_SPEC.md`: retired ZIP upload/runtime contract (retire note pending Phase 4).