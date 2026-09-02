# Git Module Installation Development Plan

> Status: Proposed
> Document version: 1.0
> Last updated: 2026-09-02
> Owner: mcp-daemon-engine

## 1. Goal

Change MCP Python module deployment from ZIP upload plus runtime `sys.path` extraction to Git-based installation while keeping S3 as a legacy deployment path until migration is complete.

The target workflow is:

1. An admin provides a Git repository URL, optional ref, module name, and package name.
2. The daemon validates and installs the package from Git into a controlled local install/cache directory.
3. The daemon records the locally installed version state: requested ref, resolved commit, optional package version, install target, and install timestamp.
4. The daemon reads the package manifest from `mcp_configuration.json` or `module.MCP_CONFIGURATION`.
5. The daemon persists tools, resources, prompts, module links, modules, and settings through the existing model loader.
6. Runtime execution imports the installed package through a deterministic import path, without S3 ZIP download or ad hoc ZIP extraction.
7. A refresh check compares the local version state with Git and reinstalls only when the configured upstream version has changed or the local install is missing/corrupt.

The existing external MCP proxy flow stays separate and continues to use `source="external"`.

Target source model:

| Source | Status | Meaning |
| ------ | ------ | ------- |
| `git` | Primary | Python MCP modules installed from Git into local artifact storage. |
| `external` | Keep | Remote MCP servers proxied over HTTP. |
| `local` / empty | Keep | Built-in or environment-installed development modules. |
| `s3` | Legacy, enabled by default | ZIP package deployment kept behind `ENABLE_S3_PACKAGE_UPLOAD=true` until retirement. |

## 2. Current State and S3 Compatibility Scope

The current package flow is implemented and documented in `docs/MCP_PACKAGE_UPLOAD_SPEC.md`.

| Area | Current behavior |
| ---- | ---------------- |
| Upload API | `generateMcpPackageUploadUrl` creates a presigned S3 PUT URL for `{packageName}.zip`. |
| Process API | `processMcpPackage` downloads the uploaded ZIP from S3, validates it, extracts it, persists the manifest, and warms cache. |
| Base64 shortcut | `loadMcpConfiguration(packageBase64: ...)` decodes a ZIP, uploads it to S3, then uses the same process path. |
| Runtime package source | `MCPModule.source` controls dispatch. `source="s3"` triggers ZIP download/extract. `source=""` imports directly from daemon `sys.path`. `source="external"` uses the built-in external proxy adapter. |
| Runtime import path | `_download_and_extract_package()` downloads `{packageName}.zip` from S3 into `Config.funct_zip_path` and extracts it to `Config.funct_extract_path`. `_get_module()` inserts `Config.funct_extract_path` into `sys.path` and imports `module_name`. |
| Manifest loading | Upload processing prefers archive-root `mcp_configuration.json` and falls back to importing `module.MCP_CONFIGURATION`. |
| Persistence | `load_mcp_configuration_into_models()` already accepts an explicit manifest dict and can persist it without importing the module. |

S3 remains supported during the Git migration. Add an explicit feature flag so retirement can be controlled without changing the Git implementation:

| Surface | Compatibility action |
| ------- | ----------------- |
| `ENABLE_S3_PACKAGE_UPLOAD` | New flag, configured default `true` for now, effective only when S3 is configured. Controls S3 upload/package processing API availability. |
| `generateMcpPackageUploadUrl` | Keep registered while `ENABLE_S3_PACKAGE_UPLOAD=true`; return `ok=false` when disabled. |
| `processMcpPackage` | Keep registered while `ENABLE_S3_PACKAGE_UPLOAD=true`; return `ok=false` when disabled. |
| `loadMcpConfiguration(packageBase64: ...)` | Keep while the flag is true; reject Base64 ZIP loading when disabled. |
| `_download_and_extract_package()` | Keep for legacy `source="s3"` runtime rows until all rows are migrated. |
| `source="s3"` | Keep supported during migration. Later retirement should raise a clear migration-required error. |
| `FUNCT_BUCKET_NAME` for modules | Still required only when S3 upload/runtime support is enabled or active rows use `source="s3"`. If missing, the effective S3 upload flag must be false. |

## 3. Constraints and Existing Extension Points

- `MCPModule` currently stores only `module_name`, `package_name`, `classes`, `source`, and audit timestamps in both DynamoDB and PostgreSQL.
- `MCPSetting.setting` is flexible JSON and can store installation metadata without a table migration.
- `MCPFunction.data` is flexible JSON and already stores extra metadata such as `external_name`.
- The dispatcher already has a source switch in `_get_module()`. This should become a provider switch for `external`, `git`, `s3`, and local direct imports. `s3` remains legacy and should be rejected only after the final retirement phase.
- `load_mcp_configuration_into_models()` is the right persistence sink and should be reused.
- The codebase has no committed test suite convention beyond `pyproject.toml` pytest settings and `ruff`; add focused tests if introducing a test runner is acceptable.

## 4. Proposed Design

Add a Git package source with `MCPModule.source == "git"`.

Git install metadata should be stored in the module setting row initially:

```json
{
  "git_url": "https://github.com/org/repo.git",
  "git_ref": "v1.2.3",
  "git_subdirectory": "",
  "install_target": "/tmp/packages/<packageName>/<resolvedCommit>",
  "install_mode": "pip",
  "version_strategy": "ref",
  "installed_package_version": "1.2.3",
  "latest_remote_version": "1.2.4",
  "resolved_commit": "<commit-sha>",
  "last_checked_commit": "<commit-sha>",
  "last_checked_at": "<utc timestamp>",
  "installed_at": "<utc timestamp>"
}
```

This avoids a schema migration for the first implementation. If the system later needs list/filter queries by repository URL, ref, or commit, add explicit columns to `MCPModule` or a separate module deployment table.

The preferred installer should be `pip install --target <install_target> git+<url>@<ref>` because it honors normal Python packaging metadata and dependencies. For repositories that contain the package in a subdirectory, support the pip direct URL fragment:

```text
git+https://github.com/org/repo.git@ref#subdirectory=path/to/package
```

The daemon should not clone arbitrary code into the current working tree, and it should not install packages into the daemon's own environment by default.

Keep S3 and Git package artifacts in separate roots:

```text
S3 legacy extraction: Config.funct_extract_path, default /tmp/functs
Git installations:   Config.git_install_path, default /tmp/packages
```

Git install targets should be commit-scoped:

```text
{Config.git_install_path}/{packageName}/{resolvedCommit}/
```

This preserves the current S3 extraction behavior while giving Git packages a separate lifecycle, dependency tree, and cleanup boundary.

## 5. Version and Refresh Model

The Git provider needs two separate concepts:

| Concept | Meaning |
| ------- | ------- |
| Requested version | What the admin asked to track, such as `gitRef=v1.2.3`, `gitRef=main`, or a semantic version tag. |
| Installed version | What is currently installed locally, represented by `resolved_commit`, optional package metadata version, and `installed_at`. |
| Remote version | What Git currently resolves for the requested version, represented by `remote_commit` and optional latest tag/package version. |

Recommended first implementation: use commit SHA comparison as the authoritative refresh decision.

Decision rules:

1. If `install_target` is missing, reinstall.
2. If `resolved_commit` is missing, reinstall and persist the resolved commit.
3. If `git_ref` is an immutable commit SHA and it equals `resolved_commit`, do not reinstall.
4. If `git_ref` is a branch or tag, run `git ls-remote <url> <ref>` and compare the returned commit with `resolved_commit`.
5. If the remote commit differs from `resolved_commit`, reinstall into a new target directory, validate the manifest, persist metadata, purge import cache, and warm MCP configuration cache.
6. If the remote commit is unchanged, return a no-op result and update only `last_checked_at` / `last_checked_commit`.
7. If Git cannot be reached, keep the current local installation and report the check failure. Do not delete a working local install before a replacement has been successfully installed and validated.

The install target should be derived from the resolved commit, not the moving ref:

```text
{git_install_path}/{packageName}/{resolvedCommit}/
```

That allows two versions to coexist during refresh. Once the new version is installed, validated, and persisted, older commit directories for the same package can be removed if no active module setting references them.

Semantic version support should be layered on top of commit comparison:

- `version_strategy="ref"`: track the explicitly supplied `gitRef`. This is the default and safest behavior.
- `version_strategy="latest_tag"`: discover the latest allowed tag from Git, resolve that tag to a commit, then compare with `resolved_commit`.
- `version_strategy="package_version"`: after install, read the installed package version from `importlib.metadata.version(distribution_name)` when a distribution name is supplied.

For the first release, implement `ref` and optionally `latest_tag`. Defer package-version comparison unless the package/distribution naming convention is clear, because Python import module names and package distribution names can differ.

## 6. New Configuration

Add settings/environment variables for installation control:

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `git_install_path` / `GIT_INSTALL_PATH` | `/tmp/packages` | Parent directory for installed Git packages. |
| `git_clone_path` / `GIT_CLONE_PATH` | `/tmp/mcp_git_repos` | Optional clone/cache directory if clone-based validation is needed. |
| `git_install_timeout` / `GIT_INSTALL_TIMEOUT` | `300` | Max seconds for one install operation. |
| `git_allowed_hosts` / `GIT_ALLOWED_HOSTS` | `github.com` | Comma-separated allowlist for Git hosts. |
| `git_token` / `GIT_TOKEN` | unset | Optional token for private repositories. Do not persist it in module settings. |
| `git_require_ref` / `GIT_REQUIRE_REF` | `true` | Require branch, tag, or commit instead of accepting moving default branch. |
| `git_refresh_policy` / `GIT_REFRESH_POLICY` | `manual` | `manual`, `on_startup`, or `on_runtime_miss`. |
| `git_refresh_ttl` / `GIT_REFRESH_TTL` | `3600` | Minimum seconds between remote version checks for the same module. |
| `git_version_strategy` / `GIT_VERSION_STRATEGY` | `ref` | Default version discovery strategy for Git modules. |
| `git_tag_pattern` / `GIT_TAG_PATTERN` | unset | Optional regex for tags when `version_strategy="latest_tag"`. |
| `enable_s3_package_upload` / `ENABLE_S3_PACKAGE_UPLOAD` | `true` when S3 is configured, otherwise effective `false` | Keep legacy S3 upload/package processing enabled during migration. |

`Config._set_parameters()` should read these values, and `Config.initialize()` should create the install/cache directories.

S3 compatibility should use an effective flag rather than the raw configured value:

```python
Config.enable_s3_package_upload = bool(setting.get("enable_s3_package_upload", True))
Config.enable_s3_package_upload = (
    Config.enable_s3_package_upload
    and bool(Config.funct_bucket_name)
    and Config.aws_s3 is not None
)
```

If S3 is not configured, `generateMcpPackageUploadUrl`, `processMcpPackage`, and the Base64 ZIP branch should return disabled/configuration errors even when `ENABLE_S3_PACKAGE_UPLOAD` was not explicitly set to `false`.

## 7. GraphQL API

Add a new mutation rather than overloading `processMcpPackage`:

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
2. Validate `gitUrl` scheme and host against the allowlist.
3. Require `gitRef` when `git_require_ref` is enabled.
4. Resolve the requested ref or discovered version to a commit SHA before persistence.
5. Compare the remote commit with the locally persisted `resolved_commit` unless `forceRefresh=true`.
6. Return `action="noop"` if the local installation exists and the remote commit has not changed.
7. Install the package into a deterministic target directory based on URL, resolved commit, subdirectory, and package name.
8. Load and validate the manifest.
9. Persist through `load_mcp_configuration_into_models(..., source="git")`.
10. Store Git install/version metadata in the setting row by predeclaring metadata keys in the manifest's module `setting`, then passing concrete values via `variables`.
11. Clear and warm `Config.mcp_configuration` for the active partition key.
12. Return `action="installed"`, `action="refreshed"`, or `action="noop"`.

Add a second mutation for version checks without requiring a full install request:

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

`checkMcpGitPackageVersion` should read the module's setting metadata from the existing cached configuration or repository row, perform a remote check when TTL allows, update `last_checked_at` metadata, and report whether refresh is needed. It should not reinstall.

Optionally add a third mutation for explicit refresh by module:

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

Register the mutation in:

- `mcp_daemon_engine/mutations/mcp_git.py`
- `mcp_daemon_engine/schema.py`
- `mcp_daemon_engine/main.py` deployment metadata

Keep legacy S3 mutations registered while `ENABLE_S3_PACKAGE_UPLOAD=true`:

- `mcp_daemon_engine/schema.py`
- `mcp_daemon_engine/main.py` deployment metadata

When `ENABLE_S3_PACKAGE_UPLOAD=false`, the S3 mutations should remain schema-compatible but return `ok=false` with a message that S3 package upload is disabled and Git installation should be used.

## 8. Installer Handler

Create `mcp_daemon_engine/handlers/mcp_git.py`.

Recommended public entry point:

```python
def install_mcp_package_from_git(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Dict[str, Any]:
    ...
```

Recommended helper responsibilities:

| Helper | Responsibility |
| ------ | -------------- |
| `_validate_git_url()` | Enforce HTTPS/SSH policy, host allowlist, and no local file URLs. |
| `_build_pip_direct_url()` | Convert URL/ref/subdirectory into a pip-compatible direct URL. |
| `_install_to_target()` | Run pip with timeout into a clean target directory. |
| `_resolve_commit()` | Resolve ref to immutable commit SHA using `git ls-remote` or post-install package metadata when available. |
| `_get_local_install_state()` | Read persisted setting metadata and verify whether `install_target` exists. |
| `_get_remote_version_state()` | Resolve current remote commit and optional latest tag/package version. |
| `_needs_refresh()` | Compare local and remote state while honoring TTL and `forceRefresh`. |
| `_read_distribution_version()` | Read installed distribution version via `importlib.metadata` when `distributionName` is configured. |
| `_load_manifest_from_install()` | Prefer `mcp_configuration.json`; fall back to importing `module.MCP_CONFIGURATION` from the install target. |
| `_clear_install_target()` | Safely remove only paths under `git_install_path`. |
| `_promote_install()` | Atomically switch from old install target to the newly validated target. |

Use `subprocess.run()` with argument lists, not shell strings, for `pip` and `git` calls.

Refresh safety rule: install into a temporary or commit-scoped target first. Only update persisted metadata and import cache after installation and manifest validation succeed. Keep the previous target usable until the new target is ready.

## 9. Runtime Loader Changes

Refactor `_get_module()` in `handlers/mcp_utility.py` into explicit provider branches:

```python
source_key = (source or "local").lower()

if source_key == "external":
    ...
elif source_key == "git":
    return _import_git_module(package_name, module_name, module_setting)
elif source_key == "s3":
    return _import_s3_module(package_name, module_name)
elif source_key == "local":
    return importlib.import_module(module_name)
else:
    raise Exception(f"Unsupported MCP module source: {source}")
```

This should intentionally change the ambiguous legacy behavior where `source is None` means direct import but `source == ""` falls through to the extracted-package path. After the refactor, both `None` and `""` should mean `local` direct import. Legacy ZIP package rows should use explicit `source="s3"` until they are migrated to `source="git"`.

The current runtime module dict contains `setting`, so either pass the module setting into `_get_module()` or add a small resolver that looks up the setting-derived `install_target` for Git modules.

For `source="git"`:

1. Check whether the expected `install_target` exists.
2. If missing and install metadata is complete, reinstall from Git.
3. Insert `install_target` into `sys.path`.
4. Import from the install target using the same cache-purge approach as `_import_module_from_extract_path()`.

Runtime should not check Git on every tool call by default. Use this policy:

- `git_refresh_policy="manual"`: runtime imports the persisted local installation and only reinstalls when the target is missing.
- `git_refresh_policy="on_runtime_miss"`: runtime reinstalls only when the target is missing or import fails due to missing module files.
- `git_refresh_policy="on_startup"`: a startup hook checks all Git modules subject to `git_refresh_ttl`; changed modules are refreshed before serving traffic when feasible.

Avoid `on_every_call`; remote checks add latency, create Git rate-limit risk, and make tool execution depend on network availability.

Do not install Git packages into `Config.funct_extract_path`. Keep S3 extraction under `/tmp/functs` and Git installations under `/tmp/packages` so the two deployment mechanisms do not share dependency files or import roots.

## 10. Manifest Contract

Git packages should support the same manifest shape as uploaded ZIP packages:

```text
repo/
|-- pyproject.toml
|-- mcp_configuration.json
|-- package_module/
    |-- __init__.py
```

The manifest should declare module settings keys that may be overridden by deployment variables. To store Git metadata through the existing loader, the handler can inject metadata defaults into each module before validation/persistence:

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

Then pass actual values through `variables` so the loader's existing override logic fills them.

## 11. Security Requirements

- Default to HTTPS Git URLs. SSH support should be explicit because it depends on host machine keys.
- Require a ref by default. Moving branches such as `main` should be allowed only when explicitly configured.
- Resolve and persist the commit SHA used for installation.
- For branch or latest-tag tracking, compare remote and local commits before refresh; do not trust a semantic version string alone.
- Never log tokens or embed `GIT_TOKEN` in persisted settings.
- Restrict install and clone paths to configured parent directories before deleting or replacing them.
- Reject local paths, `file://` URLs, and unsupported hosts.
- Prefer `pip --target` over global environment installs.
- Consider adding a future `git_allowed_repositories` allowlist for production.

## 12. S3 Compatibility, Retirement, and Migration

S3 remains enabled by default in the first Git release:

```text
ENABLE_S3_PACKAGE_UPLOAD=true
```

Required migration before hard removal:

1. Inventory all active `MCPModule` rows where `source="s3"` or where `source` is truthy and not `git`/`external`.
2. Build an operator-provided mapping from each legacy `packageName`/`moduleName` to `gitUrl`, `gitRef`, optional `gitSubdirectory`, optional `distributionName`, and version strategy.
3. For each row, run `installMcpPackageFromGit` or a batch migration helper.
4. Verify the persisted module row is now `source="git"` and contains `resolved_commit` plus `install_target`.
5. Execute at least one tool/resource/prompt per migrated module.
6. Set `ENABLE_S3_PACKAGE_UPLOAD=false` after no clients need the upload flow.
7. Keep runtime `source="s3"` support until no active module rows use it.
8. In the final retirement release, remove the S3 runtime branch or change it to raise a clear retirement error.
9. Stop requiring `FUNCT_BUCKET_NAME` for module deployment.

Do not reinterpret old `source="s3"` rows as Git rows. The runtime behavior should remain explicit and source-specific.

Hard retirement is acceptable only if there are no production rows that still require `source="s3"` or if every affected module has a known Git repository/ref and can be migrated in the same release.

## 13. Implementation Phases

### Phase 1: Foundations

- Add Git install settings to `Config`.
- Add `mcp_git.py` handler with URL validation, direct URL construction, and install target path generation.
- Add local/remote version state structs and commit comparison helpers.
- Add manifest loading from an installed target.
- Add unit-level tests for URL validation and target path safety if a test framework is accepted.

### Phase 2: Install Mutation

- Add `InstallMcpPackageFromGit` mutation.
- Add `CheckMcpGitPackageVersion` mutation.
- Add `RefreshMcpGitPackage` mutation if explicit refresh should be one call.
- Register it in `schema.py` and `main.py`.
- Reuse `validate_manifest()` and `load_mcp_configuration_into_models()`.
- Clear and warm the partition cache after successful persistence.
- Return `resolvedCommit`, installed package version, and action in the payload.

### Phase 3: Runtime Git Source and S3 Compatibility

- Add explicit `source="git"` branch in `_get_module()`.
- Import from `install_target` rather than `Config.funct_extract_path`.
- Reinstall on missing target only when install metadata is complete.
- Honor `git_refresh_policy` and `git_refresh_ttl`; avoid remote checks on every execution.
- Preserve `source="external"`, `source="s3"`, and local direct import behavior.
- Normalize the S3 branch behind an explicit `_import_s3_module()` helper so later removal is straightforward.

### Phase 4: Migration and Operations

- Create an operator-run migration helper that maps existing packages to Git URLs and refs.
- Add docs for package repository layout and install mutation examples.
- Add logging/metrics around install duration, resolved commit, version checks, cache hits, no-op checks, and reinstall attempts.
- Add rollback instructions based on keeping `ENABLE_S3_PACKAGE_UPLOAD=true` and leaving `source="s3"` rows unchanged until their Git install is verified.

### Phase 5: S3 Cleanup

- Update `docs/MCP_PACKAGE_UPLOAD_SPEC.md` with retired status.
- Change the default for `ENABLE_S3_PACKAGE_UPLOAD` to `false`, then remove it in a later release.
- Remove upload actions from `deploy()`.
- Remove GraphQL schema registration for upload mutations.
- Remove Base64 ZIP handling from `loadMcpConfiguration`.
- Remove unused S3 package helpers from `mcp_handlers.py` and `mcp_utility.py`.
- Keep S3 client initialization only if another feature still needs it.

## 14. Acceptance Criteria

- `installMcpPackageFromGit` installs a public Git package at a pinned tag or commit.
- The mutation reads `mcp_configuration.json` from the installed package and persists the expected rows.
- The mutation can fall back to `module.MCP_CONFIGURATION` when no manifest file exists.
- Persisted modules use `source="git"` and include Git install metadata in their settings.
- Persisted modules include local version state: requested ref, resolved commit, optional package version, install target, and install/check timestamps.
- A version check reports `needsRefresh=false` when Git resolves the requested ref to the installed commit.
- A version check reports `needsRefresh=true` when Git resolves the requested branch/tag/latest tag to a different commit.
- Refresh installs a changed version into a new target, validates it, updates metadata, purges import cache, and warms configuration cache.
- Refresh returns a no-op result when the remote commit is unchanged and the local installation exists.
- Runtime tool/resource/prompt execution imports from the commit-scoped `install_target` under `/tmp/packages`, not from the S3 extraction root `/tmp/functs`.
- Restarting the daemon can execute already-installed Git modules without reinstalling.
- If the install target is missing after restart or container replacement, runtime can reinstall from persisted Git metadata.
- If Git is unreachable during a version check, the existing local installation remains active.
- `source="external"` proxy modules continue to work unchanged.
- `source="s3"` modules continue to execute while S3 compatibility is enabled.
- When `ENABLE_S3_PACKAGE_UPLOAD=false`, S3 upload/package processing mutations return clear disabled messages without breaking GraphQL schema compatibility.
- When `FUNCT_BUCKET_NAME` or the S3 client is missing, S3 upload/package processing is effectively disabled even if `ENABLE_S3_PACKAGE_UPLOAD` is configured as true.
- Private repository installs work when `GIT_TOKEN` is configured and do not leak the token to logs or database rows.
- Unsupported Git hosts, missing refs when required, invalid package names, and unsafe install paths fail with clear GraphQL error messages.

## 15. Test Plan

Recommended tests:

| Test | Target |
| ---- | ------ |
| Validate Git URL | Accept allowed HTTPS Git URLs; reject `file://`, local paths, unsupported hosts, and malformed URLs. |
| Require ref | Verify `git_require_ref=true` rejects empty `gitRef`. |
| Build direct URL | Verify ref and subdirectory are encoded into the pip direct URL. |
| Install target safety | Verify generated paths stay under `git_install_path`; replacement refuses paths outside it. |
| Resolve remote commit | Mock `git ls-remote`; verify branch/tag refs resolve to commit SHAs. |
| No-op version check | Local `resolved_commit` matches remote commit and install target exists; assert no reinstall. |
| Refresh-needed check | Remote commit differs; assert `needsRefresh=true`. |
| TTL behavior | Recent `last_checked_at` skips remote check unless `forceCheck=true`. |
| Failed remote check | Git check failure leaves local install active and reports a clear error. |
| Manifest file load | Load `mcp_configuration.json` from a fake installed package. |
| Manifest import fallback | Import `module.MCP_CONFIGURATION` from a fake install target with cache restoration. |
| Mutation success | Mock installer and loader; assert `source="git"`, version metadata injection, cache clear, and cache warm. |
| Refresh mutation no-op | Mock unchanged remote commit; assert loader is not called. |
| Refresh mutation changed | Mock changed remote commit; assert new target install, manifest validation, loader call, cache purge, and metadata update. |
| Runtime import | `_get_module(..., source="git")` imports from install target and does not call S3. |
| Runtime reinstall | Missing install target triggers reinstall when metadata is present. |
| External compatibility | Existing `source="external"` branch remains covered. |
| S3 compatibility flag | `ENABLE_S3_PACKAGE_UPLOAD=true` allows legacy upload/package processing; `false` returns disabled responses. |
| S3 missing config | Missing `FUNCT_BUCKET_NAME` or S3 client makes the effective S3 upload flag false. |
| S3 runtime compatibility | `source="s3"` continues to load legacy packages until final retirement. |

## 16. Open Decisions

- Should production allow moving branch refs, or require immutable tags/commit SHAs only?
- Should `latest_tag` be supported in the first release, and if so what tag pattern should define eligible release tags?
- Should refresh checks happen only through admin GraphQL calls, or should there be startup refresh for selected deployments?
- Should package distribution name be required when reading `installed_package_version`, or should package version be informational only?
- Should private repositories be supported only through `GIT_TOKEN`, or also through SSH deploy keys?
- Should dependency installation be allowed for module packages, or should packages be required to vendor/runtime-declare only pure-Python dependencies?
- Should install metadata stay in `MCPSetting.setting`, or should a future `MCPModuleDeployment` table track repo URL, ref, commit, status, and errors?
- Should runtime reinstall be allowed automatically, or should missing packages fail until an admin re-runs the install mutation?
- How long should `ENABLE_S3_PACKAGE_UPLOAD=true` remain the default before switching to `false`?

## 17. Reference Map

- `mcp_daemon_engine/handlers/mcp_handlers.py`: existing manifest validation, ZIP processing, Base64 flow, and model-loading sink.
- `mcp_daemon_engine/handlers/mcp_utility.py`: runtime source dispatch and dynamic module import.
- `mcp_daemon_engine/handlers/config.py`: daemon settings, function paths, AWS clients, and MCP configuration cache.
- `mcp_daemon_engine/mutations/mcp_upload.py`: legacy upload mutations to keep during compatibility period.
- `mcp_daemon_engine/mutations/mcp_configuration.py`: legacy inline configuration/Base64 entry point.
- `mcp_daemon_engine/models/dynamodb/mcp_module.py`: DynamoDB module persistence with `source`.
- `mcp_daemon_engine/models/postgresql/mcp_module.py`: PostgreSQL module persistence with `source`.
- `mcp_daemon_engine/schema.py`: Graphene mutation registration.
- `mcp_daemon_engine/main.py`: SilvaEngine deployment metadata and GraphQL dispatch entry point.
- `docs/MCP_PACKAGE_UPLOAD_SPEC.md`: current ZIP upload/runtime contract.



