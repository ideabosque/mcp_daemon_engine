# Capability Engine MCP Integration Plan

> Status: Phases 1–3 implemented on `feature/integrate-capability-engine` (compatibility read views, registration mutations, invocation adapter, `builtin_modules` config parsing). Phase 4 (Banyanos cutover) and Phase 5 (cleanup) pending.
> Document version: 0.3
> Last updated: 2026-09-09
> Owner: mcp-daemon-engine

## 1. Goal

Move Banyanos `capability_engine` MCP management onto `mcp_daemon_engine` without changing the daemon foundation backend.

The daemon already owns the durable MCP catalog and execution model through:

- `MCPModule` for executable module/provider registrations.
- `MCPFunction` for tools, resources, and prompts.
- `MCPSetting` for module/provider connection settings.
- `MCPFunctionCall` for execution audit records.
- Existing upload, Git install, external sync, cache, and runtime dispatch handlers.

This plan adds a compatibility GraphQL layer on top of those existing primitives so Banyanos can stop storing and invoking MCP servers through `capability_engine` tables and transports.

## 2. Non-Goals

- No changes to the daemon persistence foundation, repository abstraction, DynamoDB/PostgreSQL table shapes, or runtime dispatcher core.
- No direct dependency from `mcp_daemon_engine` back to `banyanos.capability_engine`.
- No migration of Banyanos Skill, AG-UI, authorization policy, or dashboard features in this phase.
- No attempt to preserve Banyanos internal UUIDs as daemon primary keys unless a later migration requires an alias map.
- No new transport implementation. Remote MCP uses the daemon external MCP proxy; custom/uploaded MCP uses the daemon package loader.

## 3. Capability Engine Findings

`banyanos/capability_engine` currently treats Provider and MCP Server as the same concept backed by `tenant_capability_mcp_server`.

Important current concepts:

| Capability engine concept | Current storage/API | Notes |
| --- | --- | --- |
| Provider | `tenant_capability_mcp_server`, `providerDetail`, `providerList`, `createProvider`, `updateProvider`, `deleteProvider` | Provider is the rich MCP server configuration view. |
| MCP Server | Same table, `mcpServerDetail`, `mcpServerList`, `createMcpServer`, `syncMcpServerTools` | Sync-focused view over the same Provider row. |
| MCP Tool | `tenant_capability_mcp_tool`, `toolDetail`, `toolList`, `discoverTools`, `loadTools` | Tools are discovered through MCP transport and stored per server. |
| Invocation | `tenant_capability_tool_invocation`, `invokeTool`, `testTool` | Wraps authorization, rate limit, circuit breaker, transport call, logs, and audit. |
| Remote MCP | Provider `config.transport = REMOTE` and HTTP MCP transport | Maps directly to daemon external MCP. |
| Custom MCP | Provider `config.transport = CUSTOM` or legacy `STDIO`/`INTERNALIZABLE`, plus uploaded package config | Maps to daemon internal uploaded module. |
| Built-in tool | Provider `config.transport = BUILTIN` or `builtin://...` endpoint | Maps to system MCP modules installed into the daemon catalog. |

The capability engine GraphQL surface is broader than MCP management. The replacement scope is only Provider/MCP Server/Tool/Invocation behavior that represents MCP management and execution.

## 4. Target Mapping

| Banyanos capability engine | Daemon target | `MCPModule.source` | Existing daemon path |
| --- | --- | --- | --- |
| Remote MCP | External MCP server | `"external"` | `syncExternalMcpServer` writes rows and runtime dispatches through `ExternalMCPProxy`. |
| Custom MCP (uploaded ZIP) | Internal MCP backed by S3-hosted package | `"s3"` | `generateMcpPackageUploadUrl` → S3 PUT → `processMcpPackage`, or the Base64 shortcut via `loadMcpConfiguration(packageBase64: ...)` (gated by `Config.enable_s3_package_upload`). |
| Custom MCP (Git-installed) | Internal MCP backed by a Git repo | `"git"` | `installMcpPackageFromGit` in `handlers/mcp_git.py` clones, pip-installs, loads the manifest, and writes rows with `MCPSetting.setting.git_url`, resolved commit, and optional TTL. `checkMcpGitPackageVersion` and `refreshMcpGitPackage` maintain freshness. |
| Custom MCP provider | Uploaded/Git-installed MCP module/package | `"s3"` or `"git"` | `MCPModule.package_name`, `MCPModule.module_name`, `MCPSetting.setting`, and module `classes`. |
| Built-in tool | System MCP module serving the platform | Whatever install path was used (`"s3"` / `"git"` / `""` for in-image) | Install through any normal path, then declare the module name in the deployment config `builtin_modules` list (see §10). No storage marker, no promotion mutation. |
| Tool | MCP function with `mcp_type = "tool"` | n/a | `MCPFunction.name`, `description`, `data.inputSchema`, module/class/function link fields, `status` (`0` = disabled, `1` = enabled per migration 0005). |
| Runtime log/invocation | MCP function call | n/a | `MCPFunctionCall` plus existing `execute_decorator()` audit updates. |

**Key idea**: `MCPModule.source` describes the *install/transport origin* (external HTTP, S3, Git, or locally-present). Built-in status is *orthogonal* and lives **only in deployment configuration** — `Config.setting["builtin_modules"]`, a list of module names. Compatibility read views resolve `BUILTIN` by membership in that list at query time; nothing is written to storage. Ops controls the classification through env/config at deploy time, not through the runtime API.

## 5. Proposed Additive GraphQL Layer

Add daemon-native compatibility files without disturbing existing schema fields:

```text
mcp_daemon_engine/
|-- types/capability_mcp.py
|-- queries/capability_mcp.py
`-- mutations/capability_mcp.py
```

Register them in `schema.py` as additional Query and Mutation fields. The current daemon fields remain unchanged.

The compatibility layer should return Banyanos-shaped objects where practical, but it should store only daemon-native rows. This lets Banyanos clients migrate endpoint-by-endpoint while the daemon remains the system of record.

## 6. Proposed Query Contract

### 6.1 Provider/MCP Server Views

```graphql
type CapabilityMcpProvider {
    id: ID!
    name: String!
    displayName: String!
    description: String
    endpoint: String
    transport: CapabilityMcpTransport!
    authType: String
    protocolVersion: String
    status: String!
    registerStatus: String!
    toolCount: Int!
    config: JSONCamelCase
    createdAt: DateTime
    updatedAt: DateTime
    updatedBy: String
}

enum CapabilityMcpTransport {
    REMOTE
    CUSTOM
    BUILTIN
}

type CapabilityMcpProviderConnection {
    capabilityMcpProviderList: [CapabilityMcpProvider]
    pageNumber: Int
    pages: Int
    total: Int
}
```

Add fields:

```graphql
extend type Query {
    capabilityMcpProvider(id: ID!): CapabilityMcpProvider
    capabilityMcpProviderList(
        pageNumber: Int
        limit: Int
        transport: CapabilityMcpTransport
        status: String
        keyword: String
    ): CapabilityMcpProviderConnection
    capabilityMcpServer(id: ID!): CapabilityMcpProvider
    capabilityMcpServerTools(id: ID!, pageNumber: Int, limit: Int): CapabilityMcpToolConnection
}
```

Implementation:

- `id` resolves to daemon `MCPModule.module_name` for provider/server views.
- Transport mapping — the deployment-config check comes first because it can promote a row that would otherwise be `CUSTOM`:
  1. If `module_name in Config.setting["builtin_modules"]` (a list configured at deploy time; see §10) → `BUILTIN` (endpoint `builtin://{module_name}`).
  2. Else if `MCPModule.source == "external"` → `REMOTE` (read `MCPSetting.setting.base_url` / `bearer_token` / `headers` / `name_prefix` / `timeout`).
  3. Else → `CUSTOM`. Endpoint URI depends on `source`: `s3://{funct_bucket_name}/{package_name}.zip` for `"s3"`; `MCPSetting.setting.git_url` (with `@{commit}` appended when resolved) for `"git"`; `null` with `config.source_raw` set for anything else.
- `toolCount` counts `MCPFunction` rows with `mcp_type == "tool"`, `status != 0`, and matching `module_name`.

### 6.2 Tool Views

```graphql
type CapabilityMcpTool {
    id: ID!
    providerId: ID!
    mcpServerId: ID!
    name: String!
    displayName: String!
    description: String
    inputSchema: JSONCamelCase
    outputSchema: JSONCamelCase
    status: String!
    isDeprecated: Boolean!
    config: JSONCamelCase
    createdAt: DateTime
    updatedAt: DateTime
}
```

Add fields:

```graphql
extend type Query {
    capabilityMcpTool(id: ID!): CapabilityMcpTool
    capabilityMcpToolList(
        pageNumber: Int
        limit: Int
        providerId: ID
        status: String
        keyword: String
    ): CapabilityMcpToolConnection
}
```

Implementation:

- `id` maps to `MCPFunction.name`.
- `providerId` and `mcpServerId` both map to `MCPFunction.module_name`.
- `status` maps from `MCPFunction.status`: `1` or missing is `AVAILABLE`, `0` is `UNAVAILABLE`.
- `inputSchema` reads `MCPFunction.data.inputSchema`.
- Preserve daemon-only details in `config`, including `class_name`, `function_name`, `return_type`, `source`, `external_name`, and `is_async`.

## 7. Proposed Mutation Contract

### 7.1 Remote MCP Registration

```graphql
extend type Mutation {
    registerCapabilityRemoteMcp(
        serverName: String!
        displayName: String
        description: String
        baseUrl: String!
        bearerToken: String
        headers: JSONSnakeCase
        namePrefix: String
        updatedBy: String!
    ): CapabilityMcpRegistrationPayload
}
```

Implementation:

- Wrap existing `sync_external_mcp_server()`.
- Store display metadata in `MCPSetting.setting.capability_metadata`.
- Return the provider/server view plus sync stats.
- Do not add a separate create-then-sync split for remote MCP unless Banyanos UI requires a draft state. Daemon external MCP sync is already the useful registration action.

### 7.2 Custom MCP Upload/Registration

Use two options depending on package source.

For S3 presigned upload:

```graphql
extend type Mutation {
    generateCapabilityCustomMcpUploadUrl(
        packageName: String!
    ): GenerateMcpPackageUploadUrlPayload

    registerCapabilityCustomMcpPackage(
        s3Key: String!
        moduleName: String!
        packageName: String!
        displayName: String
        description: String
        variables: JSONCamelCase
        updatedBy: String!
    ): CapabilityMcpRegistrationPayload
}
```

For Base64 inline package:

```graphql
extend type Mutation {
    registerCapabilityCustomMcpPackageBase64(
        packageBase64: String!
        moduleName: String!
        packageName: String!
        displayName: String
        description: String
        variables: JSONCamelCase
        updatedBy: String!
    ): CapabilityMcpRegistrationPayload
}
```

Implementation:

- `generateCapabilityCustomMcpUploadUrl` delegates to `generate_upload_url()`.
- `registerCapabilityCustomMcpPackage` delegates to `process_mcp_package(source="s3")`.
- `registerCapabilityCustomMcpPackageBase64` delegates to `process_base64_package()`. Note the current `LoadMcpConfiguration.mutate` gates its Base64 branch on `Config.enable_s3_package_upload`; the compatibility mutation should either respect that flag (return a clear disabled error) or explicitly bypass it with rationale.
- Store display metadata in `MCPSetting.setting.capability_metadata` after package load if the manifest did not include it.

### 7.3 Custom MCP Git Install

```graphql
extend type Mutation {
    registerCapabilityCustomMcpGitPackage(
        gitUrl: String!
        moduleName: String!
        packageName: String
        ref: String
        subdirectory: String
        displayName: String
        description: String
        variables: JSONCamelCase
        updatedBy: String!
    ): CapabilityMcpRegistrationPayload

    refreshCapabilityCustomMcpGitPackage(
        moduleName: String!
        updatedBy: String!
    ): CapabilityMcpRegistrationPayload

    checkCapabilityCustomMcpGitPackageVersion(
        moduleName: String!
    ): CapabilityMcpGitVersionInfo
}
```

Implementation:

- `registerCapabilityCustomMcpGitPackage` delegates to `install_mcp_package_from_git()` in `handlers/mcp_git.py`. That handler resolves the ref (branch/tag/commit), pip-installs into `Config.funct_extract_path`, loads and validates the manifest, writes rows with `source="git"`, and stores `git_url` / resolved commit / TTL under `MCPSetting.setting`.
- `refreshCapabilityCustomMcpGitPackage` delegates to `refresh_mcp_git_package()` — pulls the latest ref and re-loads the manifest.
- `checkCapabilityCustomMcpGitPackageVersion` delegates to `check_mcp_git_package_version()` — reports installed vs. upstream commit without installing.
- If the git URL requires auth, the daemon reads credentials from its environment (`GIT_SSH_COMMAND`, PAT env vars) — do not accept credentials through this mutation.

### 7.4 Built-In Classification (No Runtime Mutation)

There is intentionally **no** built-in-specific registration mutation. A module becomes `BUILTIN` in the read view solely by appearing in the deployment-config list `Config.setting["builtin_modules"]` (see §10). To register a system-wide package (e.g. `mcp_a2a_proxy`):

1. Install it through the normal path that matches how it ships:
   - Git-installed → §7.3 `registerCapabilityCustomMcpGitPackage`.
   - S3-uploaded → §7.2 `registerCapabilityCustomMcpPackage` / Base64 variant.
   - Baked into the daemon image with an inline manifest → existing `loadMcpConfiguration(mcpConfiguration: ...)` (with `validate_manifest()` added for safety).
2. Ensure the module name is in `builtin_modules` in the deployment config for that environment.

Compatibility read views resolve `BUILTIN` by membership in the config list at query time — no storage write, no promotion step, no demotion mutation. Removing the module name from the config (and redeploying/hot-reloading) is the demotion path.

### 7.5 Tool Invocation

```graphql
extend type Mutation {
    invokeCapabilityMcpTool(
        name: String!
        arguments: JSONCamelCase
        agentId: String
        traceId: String
        updatedBy: String!
    ): CapabilityMcpInvocation

    testCapabilityMcpTool(
        name: String!
        arguments: JSONCamelCase
        updatedBy: String!
    ): CapabilityMcpInvocation
}
```

Implementation options:

1. Preferred: add a small daemon helper that executes an MCP function by name through the existing `mcp_utility` runtime path and returns a normalized invocation object.
2. Fallback: call the same internal JSON-RPC path the SSE/stdio server uses, if that already gives complete audit behavior.

The mutation should not reimplement Banyanos authorization, rate limiting, or circuit breaker logic in this phase. Those remain in capability/agent/orchestration layers until they are intentionally migrated. The daemon compatibility mutation should focus on execution and audit through `MCPFunctionCall`.

## 8. Data Mapping Details

### 8.1 Provider/MCP Server to Module/Setting

| Capability field | Daemon field |
| --- | --- |
| `id` | `MCPModule.module_name` |
| `name`, `server_name` | `MCPModule.module_name` |
| `display_name` | `MCPSetting.setting.capability_metadata.display_name` or module name |
| `description` | `MCPSetting.setting.capability_metadata.description` |
| `endpoint` | Built-in (in `Config.setting["builtin_modules"]`): `builtin://{module_name}`. Otherwise: `MCPSetting.setting.base_url` for external; `s3://{funct_bucket_name}/{package_name}.zip` for `source="s3"`; `MCPSetting.setting.git_url` (with `@{commit}` appended when resolved) for `source="git"`; `null` otherwise. |
| `transport` | `BUILTIN` when `module_name in Config.setting["builtin_modules"]`. Else `REMOTE` when `MCPModule.source == "external"`. Else `CUSTOM` (with `config.source_raw` echoing the raw source value for troubleshooting). |
| `auth_type` | `BEARER` when `bearer_token` exists, else `NONE`; future OAuth/API key stays inside setting headers/secrets. |
| `protocol_version` | `MCPSetting.setting.protocol_version` or daemon default. |
| `config.timeout` | `MCPSetting.setting.timeout` (seconds) — forwarded to `MCPHttpClient` in both sync and proxy paths since commit `533251f`. |
| `status` | `ACTIVE` when at least one enabled tool exists, otherwise `INACTIVE`; disabled modules use a future setting marker |
| `register_status` | `SUCCESS` when sync/package processing succeeds, `FAILED` only in mutation payload on failure |

### 8.2 Tool to Function

| Capability field | Daemon field |
| --- | --- |
| `id` | `MCPFunction.name` |
| `tool_name`, `name` | `MCPFunction.name` |
| `display_name` | `MCPFunction.data.display_name` or name |
| `description` | `MCPFunction.description` |
| `input_schema` | `MCPFunction.data.inputSchema` |
| `output_schema` | `MCPFunction.data.outputSchema` if present |
| `is_deprecated` | `MCPFunction.status == 0` |
| `sync_timeout_s` | `MCPFunction.data.sync_timeout_s` or daemon default |
| `max_payload_b` | `MCPFunction.data.max_payload_b` or daemon default |
| `tags` | `MCPFunction.data.capability_tags` |

### 8.3 Invocation to Function Call

| Capability field | Daemon field |
| --- | --- |
| `id` | `MCPFunctionCall.mcp_function_call_uuid` |
| `invocation_id` | `MCPFunctionCall.mcp_function_call_uuid` unless separate alias is added in `notes`/`arguments` |
| `tool_name` | `MCPFunctionCall.name` |
| `request_payload` | `MCPFunctionCall.arguments` |
| `response_payload` | `MCPFunctionCall.content` or S3 content reference |
| `status` | `MCPFunctionCall.status` |
| `trace_id`, `agent_id` | Store in `MCPFunctionCall.arguments._capability_context` or `notes` |
| `duration_ms` | `MCPFunctionCall.time_spent` |

## 9. Migration Strategy

### Phase 0: Contract Freeze

- Confirm Banyanos clients only need the MCP subset for replacement: Provider/MCP Server/Tool/Invocation.
- Freeze the compatibility GraphQL names in this document before implementation.
- Decide whether Banyanos should use daemon-native names directly or the compatibility names above.

### Phase 1: Read Compatibility

- Implement provider/server/tool query adapters over existing daemon repositories.
- Add filtering by transport, provider/server/module, status, and keyword.
- Add tests using mocked repositories or in-memory fakes; avoid introducing a new test runner unless agreed.

### Phase 2: Registration Mutations

- Add remote MCP registration wrapper around `sync_external_mcp_server()`.
- Add custom MCP upload wrappers around `generate_upload_url()`, `process_mcp_package()`, and `process_base64_package()` (respecting the `enable_s3_package_upload` gate).
- Add custom MCP Git wrappers around `install_mcp_package_from_git()`, `refresh_mcp_git_package()`, and `check_mcp_git_package_version()`.
- Wire the deployment-config `builtin_modules` list into `Config.setting` (parse the env var, default to empty list). Compatibility read views check this list at query time — no separate built-in registration mutation is added.
- Ensure all successful registration paths clear and warm the partition MCP configuration cache (they already do internally; the compatibility wrappers must not skip that step).

### Phase 3: Invocation Adapter

- Add `invokeCapabilityMcpTool` and `testCapabilityMcpTool`.
- Reuse existing daemon execution and audit behavior.
- Return compatibility-shaped invocation output so Banyanos callers can replace `invokeTool` reads.

### Phase 4: Banyanos Cutover

- Update capability engine or gateway callers to call daemon GraphQL for MCP management.
- Keep Banyanos Skill, AG-UI, authorization, and dashboard logic pointing to capability engine.
- Deprecate capability engine MCP transport code only after production traffic and data reads are verified against daemon outputs.

### Phase 5: Cleanup

- Remove or freeze writes to `tenant_capability_mcp_server`, `tenant_capability_mcp_tool`, and `tenant_capability_tool_invocation` for MCP data.
- Keep old queries read-only temporarily if historical dashboards still need them.
- Remove Banyanos MCP transport package/runtime only after no callers use it.

## 10. Implementation Notes

- Keep `partition_key` behavior exactly as daemon already applies it. The compatibility layer must read it from `info.context`, not from GraphQL arguments.
- Do not create a daemon-side Provider table. Provider is only a compatibility projection over `MCPModule` and `MCPSetting`.
- Do not store bearer tokens in compatibility metadata. Use existing `MCPSetting.setting.bearer_token` semantics until a separate secret-management task is planned.
- Use `JSONSnakeCase` for HTTP headers on the mutation input, matching `SyncExternalMcpServer`, so header names are preserved on write.
- **Preserve header keys on the read path too.** `JSONSnakeCase.serialize` recursively snake-cases nested dict keys, which mangles HTTP header names like `Part-Id` → `part_id` when the daemon later forwards them upstream. `Config._fetch_modules_and_settings` restores `setting["headers"]` verbatim after serialization (commit `bc0b3d4`). Compatibility read resolvers that return module settings must do the same for `headers` and any other verbatim sub-dicts, or downstream re-writes will re-introduce the bug.
- Use `JSONCamelCase` for manifest/tool schemas, matching existing daemon manifest handling.
- Prefer `module_name` as the stable server/provider identifier for new daemon-managed records. If Banyanos UUID continuity is required, store old IDs under `MCPSetting.setting.capability_metadata.legacy_ids`.
- Built-in status is a **deployment-configuration** concern, not a storage or mutation concern. `MCPModule.source` continues to describe the install/transport origin only (`"external"`, `"s3"`, `"git"`, or empty when the code is baked into the daemon image). A module is built-in **only when** its `module_name` appears in `Config.setting["builtin_modules"]` — a list read from environment/config at daemon startup (env var e.g. `BUILTIN_MCP_MODULES=mcp_a2a_proxy,mcp_platform_health`, parsed comma-separated into a list on `Config.setting`). Compatibility read-view resolvers check this list first; if absent the module falls back to `CUSTOM` (or `REMOTE` for `source="external"`).
- **Why config, not mutation**: (1) ops has authoritative, deploy-time control — no runtime API surface to abuse; (2) reproducible across fresh environments; (3) demotion is a config edit + redeploy, no cleanup mutation needed; (4) no storage marker to keep in sync with the config, so migration-safe by construction. Trade-off: promoting a module mid-cycle requires a config change (env update + hot-reload of `Config.setting`, or a restart). Acceptable for platform-scoped built-ins that don't change often.
- Do NOT write `source="builtin"` or any `capability_transport` marker to storage — no daemon code path recognizes them, and they'd desynchronize from the config list.
- The Base64 upload branch of `LoadMcpConfiguration.mutate` is gated on `Config.enable_s3_package_upload`. Compatibility mutations that wrap that branch must decide whether to inherit the gate (surface as `ok: false` with a clear disabled message) or bypass it; document the choice in the resolver.
- Per-server request timeouts (`MCPSetting.setting.timeout`, integer seconds) are forwarded to `MCPHttpClient` in both sync and proxy paths (commit `533251f`). Expose it as `config.timeout` in the provider view and accept it as an optional argument on `registerCapabilityRemoteMcp` when adding a follow-up mutation.
- `MCPFunctionCall.content` may be inlined text or an S3 reference depending on payload size — invocation view resolvers should handle both shapes without materializing large S3-hosted content unless explicitly requested.

## 11. Risks and Open Questions

| Risk/question | Recommendation |
| --- | --- |
| Banyanos uses UUID IDs, while daemon modules are name-keyed. | Use `module_name` as new ID. Add `legacy_ids` metadata only if old clients require it. |
| Banyanos invocation has authorization/rate/circuit behavior not present in daemon compatibility layer. | Keep governance outside daemon for first cutover. Migrate policy concerns separately. |
| Existing daemon inline manifest load skips validation. | Compatibility built-in/custom manifest paths should call `validate_manifest()` before persistence. |
| Built-in module classification convention is not yet formalized. | Deploy-time configuration only: `Config.setting["builtin_modules"]` (a list of module names, parsed from an env var like `BUILTIN_MCP_MODULES`). Compatibility read views check membership at query time. No storage marker, no runtime mutation. `MCPModule.source` stays describing install origin only. Any module not in the list defaults to `CUSTOM` (or `REMOTE` when `source="external"`). |
| Tool disable/delete semantics differ. | Map capability `UNAVAILABLE` to `MCPFunction.status=0`; avoid hard deletes during migration. |
| Historical runtime logs live in capability tables. | Do not migrate logs initially. Expose daemon `MCPFunctionCall` for new invocations and keep old history read-only in Banyanos if needed. |
| Custom MCP now has two registration paths (`s3`, `git`) with different failure/refresh semantics. | Model them as sibling mutations under `CUSTOM` transport, both returning the same `CapabilityMcpRegistrationPayload`. Distinguish them only in `config.source_raw` on the read view. |
| `Config.enable_s3_package_upload` may be off in some deployments, disabling the Base64 branch. | Compatibility mutation must surface a clear "disabled" `ok: false` message when the flag is off, so Banyanos clients don't silently retry. |
| Nested dict keys get snake-cased if the compatibility read resolvers naively re-serialize through `JSONSnakeCase`. | Follow the pattern in `Config._fetch_modules_and_settings` (commit `bc0b3d4`): serialize the outer dict, then restore verbatim sub-dicts for `headers` and any similar preserve-keys fields before returning. |

## 12. Acceptance Criteria

- Banyanos can register a remote MCP server through daemon GraphQL and see the synced tools through compatibility queries.
- Banyanos can register an uploaded custom MCP package (S3 presign or Base64) through daemon GraphQL and see its tools through compatibility queries.
- Banyanos can register a Git-installed custom MCP package through daemon GraphQL, refresh it against a new commit, and check its remote version.
- Banyanos can register a built-in system MCP module, such as an A2A proxy module, through daemon GraphQL and expose it as `BUILTIN`.
- Banyanos can invoke a daemon-managed MCP tool and receive a compatibility-shaped invocation record.
- Header keys (`Part-Id`, `x-api-key`, etc.) stored under `MCPSetting.setting.headers` reach both the upstream HTTP request and the compatibility read-view verbatim — no snake-case mangling at any hop.
- Existing daemon mutations and queries continue to work unchanged.
- No daemon foundation backend schema or dispatcher core changes are required.
