# Capability Engine MCP Integration Plan

> Status: Phases 1–3 implemented and verified in commit `55f04c5` on `feature/integrate-capability-engine` — 7 compatibility read queries + specific registration mutations (§7.1–7.3) + invocation adapter (§7.5) + `builtin_modules` config bootstrap. Provider deregistration added as unified `unregisterCapabilityMcp` (§7.6). Unified registration entry point added as `registerCapabilityMcp` with `transport` selector (§7.7), sharing scaffolding with the specific mutations via `_register_and_respond`. Phase 4 (Banyanos cutover) and Phase 5 (cleanup) pending.
> Document version: 0.6
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

## 5. Additive GraphQL Layer (Implemented)

Daemon-native compatibility files added by commit `55f04c5` without disturbing existing schema fields:

```text
mcp_daemon_engine/
|-- types/capability_mcp.py            (152 LOC)
|-- queries/capability_mcp.py          (604 LOC)
|-- mutations/capability_mcp.py        (619 LOC)
|-- handlers/capability_mcp_invoke.py  (182 LOC — invocation adapter)
```

Registered in `schema.py` (+148 LOC) as additional Query/Mutation fields plus `type_class()` entries, and in `main.py` (+43 LOC) `deploy()` action manifest. Registration mutations are listed in `_CONFIG_MUTATIONS`, so the daemon clears the partition MCP configuration cache after every successful compatibility registration — no manual cache handling in the wrapper code. The current daemon fields remain unchanged.

The compatibility layer returns Banyanos-shaped objects and stores only daemon-native rows. Banyanos clients can migrate endpoint-by-endpoint while the daemon remains the system of record.

## 6. Query Contract (Implemented)

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

### 6.3 Invocation History View

```graphql
type CapabilityMcpInvocation {
    id: ID!
    invocationId: ID!
    toolName: String
    requestPayload: JSONCamelCase
    responsePayload: JSONCamelCase
    status: String
    traceId: String
    agentId: String
    durationMs: Int
    createdAt: DateTime
    updatedAt: DateTime
}

extend type Query {
    capabilityMcpInvocationList(
        pageNumber: Int
        limit: Int
        toolName: String
        status: String
    ): CapabilityMcpInvocationConnection
}
```

Implementation:

- Reads `MCPFunctionCall` rows keyed by `partition_key` from `info.context`.
- `id` and `invocationId` both map to `MCPFunctionCall.mcp_function_call_uuid`.
- `toolName` = `MCPFunctionCall.name`.
- `requestPayload` = `MCPFunctionCall.arguments` (minus internal `_capability_context` if present).
- `responsePayload` = `MCPFunctionCall.content` — string when inlined, `{"s3_ref": "..."}` shape when offloaded (see §10).
- `traceId` and `agentId` come from `MCPFunctionCall.arguments._capability_context` if the invocation was created through §7.5 `invokeCapabilityMcpTool`.
- `durationMs` = `MCPFunctionCall.time_spent`.

## 7. Mutation Contract (Implemented)

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

### 7.6 Provider Deregistration (Unified)

Symmetric removal for every register path. **One mutation** handles remote, S3-uploaded, and Git-installed providers — the stored `MCPModule.source` is looked up from the row, so callers only need the module name.

```graphql
type CapabilityMcpUnregistrationPayload {
    ok: Boolean!
    message: String
    moduleName: String
    transport: String        # "remote" or "custom" — derived from stored source
    source: String           # actual MCPModule.source that was removed
    deletedFunctions: Int
    deletedSetting: Boolean
    settingKept: Boolean     # true when the shared setting was left in place
}

extend type Mutation {
    unregisterCapabilityMcp(
        moduleName: String!
        expectedTransport: String     # optional safety guard
        updatedBy: String!
    ): CapabilityMcpUnregistrationPayload
}
```

`expectedTransport` (case-insensitive) refuses the delete when the stored source doesn't match — useful for UI flows that want to confirm the caller is removing the intended kind of provider:

| Value | Restricts delete to |
| ----- | ------------------- |
| `"remote"` / `"external"` | `source="external"` only |
| `"s3"` | S3-uploaded packages only |
| `"git"` | Git-installed packages only |
| `"custom"` | S3 or Git (either kind of custom package) |
| `"builtin"` | no restriction (BUILTIN is a read-view label computed from deployment config; the underlying provider is still one of the above sources) |
| omitted / unknown | no restriction |

Implementation:

1. Look up the `MCPModule` row for `moduleName`. Return `ok: false, message: "Module 'X' not found"` when absent.
2. If `expectedTransport` was provided, verify the stored `source` is in its allowed set; refuse with a clear message otherwise.
3. Enumerate function names for the module (from the cached `Config.fetch_mcp_configuration` `tools`/`resources`/`prompts` lists; fall back to a live `mcp_function` list if the cache is empty).
4. Delete every `MCPFunction` row.
5. Delete the `MCPModule` row. If this step fails, return `ok: false` with the function count already deleted — the caller can retry.
6. Extract the shared `setting_id` from the module's `classes[0].setting_id`. Delete the `MCPSetting` row **iff** no other module still references it. Fail-safe: if the "still referenced?" check errors, we keep the setting.
7. Cache invalidation is automatic — `unregisterCapabilityMcp` is in `_CONFIG_MUTATIONS`.

**Deliberate non-actions**:

- Does not delete the extracted install dir under `funct_extract_path`. Re-registering with the same package will reuse whatever files are on disk. Matches the plan's "no automatic pruning" non-goal (§4).
- Does not delete the S3 `.zip` object. The bucket may host packages for other partitions; a per-partition mutation cannot safely reap shared objects.
- Does not touch the deployment-config `builtin_modules` list. BUILTIN classification is deploy-time (§10); removing a builtin means removing the module name from that list and redeploying, which is out of the mutation's scope.

**Design decision — why unified for unregister but both patterns for register**:

Deregister genuinely takes one required argument (`moduleName`) regardless of source, so a single mutation is the cleanest API. Register is different: each source has a distinct set of *required* arguments (`baseUrl` for remote, `s3Key` for S3, `packageBase64` for Base64, `gitUrl` for Git), so a unified register with everything optional forfeits GraphQL's schema-level required-field enforcement and needs runtime cross-argument validation.

Rather than choose, the implementation ships **both patterns** (see §7.7):

- The specific `registerCapability*` mutations (§7.1–7.3) remain the canonical form — they carry schema-level required-field enforcement, produce clear SDK signatures, and are easy to discover in GraphiQL. Recommended for typed clients and for scripts where the source is known at compile time.
- The unified `registerCapabilityMcp` (§7.7) is added as a convenience face for polymorphic callers (UI dialogs that let the operator pick a source at runtime, or single-code-path scripts iterating over heterogeneous provider descriptions). It performs runtime cross-argument validation and delegates to the same underlying handlers.

Both entry points share a single `_register_and_respond` helper in `mutations/capability_mcp.py`, so scaffolding — metadata storage, provider re-resolution, error framing, cache invalidation — lives in one place. If a future graphene release supports `@oneOf` input unions (GraphQL v17), the unified mutation can migrate to that shape without touching the specific ones.

### 7.7 Unified Registration Entry Point

Convenience face over §7.1–7.3 for polymorphic callers. Dispatches by the `transport` argument to the same underlying handlers.

```graphql
extend type Mutation {
    registerCapabilityMcp(
        # transport selector: "remote" | "s3" | "base64" | "git"
        transport: String!

        # common
        moduleName: String!
        packageName: String
        displayName: String
        description: String
        variables: JSONCamelCase
        updatedBy: String!

        # transport=remote
        baseUrl: String
        bearerToken: String
        headers: JSONSnakeCase
        namePrefix: String
        timeout: Int

        # transport=s3
        s3Key: String

        # transport=base64
        packageBase64: String

        # transport=git
        gitUrl: String
        ref: String
        subdirectory: String
        versionStrategy: String
        distributionName: String
    ): CapabilityMcpRegistrationPayload
}
```

Per-transport required arguments and delegation target:

| `transport` | Required (besides `moduleName`, `updatedBy`) | Delegates to | Gates |
| ----------- | -------------------------------------------- | ------------ | ----- |
| `"remote"` | `baseUrl` | `sync_external_mcp_server()` | — |
| `"s3"` | `s3Key` | `process_mcp_package(source="s3")` | `Config.enable_s3_package_upload` |
| `"base64"` | `packageBase64` | `process_base64_package()` | `Config.enable_s3_package_upload` |
| `"git"` | `gitUrl` | `install_mcp_package_from_git()` | — |

Behaviors:

- **Case-insensitive `transport`**. Unknown value returns `ok: false, message: "Unknown transport '…'. Supported: remote, s3, base64, git."`
- **Missing required arg** returns `ok: false, message: "<arg> is required for transport=<t>"`. No partial state is written.
- **Feature-flag gate** (`enable_s3_package_upload`) on `s3` and `base64` returns `ok: false, message: "…disabled; use transport=git."` so polymorphic callers can degrade gracefully.
- **Return payload** is the same `CapabilityMcpRegistrationPayload` used by the specific mutations (§7.1–7.3), with `resolved_commit` / `installed_package_version` / `action` populated when the underlying Git handler produces them.

When to use each entry point:

- **`registerCapabilityMcp` (unified)** — a UI form where the operator picks the source type; a batch job iterating over provider descriptions from external config; any caller where the source isn't known at compile time.
- **`registerCapability*` (specific)** — typed SDK generation (each mutation produces a signature with only the fields it needs); scripts hard-coded to one source type; when you want the GraphQL schema itself to reject a call missing a required field rather than seeing a runtime `ok: false`.

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

### Phase 1: Read Compatibility ✓ Complete (`55f04c5`)

- Provider/server/tool query resolvers landed in `queries/capability_mcp.py`, with transport mapping, endpoint URI construction, `toolCount` filtering on `status != 0`, and pagination.
- Filtering: `transport` (`REMOTE`/`CUSTOM`/`BUILTIN`), `providerId`, `status`, `keyword`.
- Also shipped: `capabilityMcpInvocationList` (documented in §6.3) reading `MCPFunctionCall`.
- Tests: none added yet — verified live against 6 providers / 38 tools before commit.

### Phase 2: Registration Mutations ✓ Complete (`55f04c5`)

- `registerCapabilityRemoteMcp` — wraps `sync_external_mcp_server()`.
- `generateCapabilityCustomMcpUploadUrl` + `registerCapabilityCustomMcpPackage` + `…PackageBase64` — wrap S3 presign / process / Base64 paths; the Base64 branch inherits the `Config.enable_s3_package_upload` gate.
- `registerCapabilityCustomMcpGitPackage` + `refresh…` + `check…Version` — wrap `install_mcp_package_from_git()` / `refresh_mcp_git_package()` / `check_mcp_git_package_version()`.
- `builtin_modules` list wired into `Config.setting` from `BUILTIN_MCP_MODULES` env var (or a `builtin_modules` settings key). Compatibility read views check this list at query time — no dedicated registration mutation added.
- `unregisterCapabilityMcp` — single unified deregister mutation (§7.6) covering remote / S3 / Git via `moduleName` + optional `expectedTransport` guard.
- `registerCapabilityMcp` — unified registration entry point (§7.7) dispatching by `transport` selector to the same underlying handlers as §7.1–7.3, for polymorphic UIs and batch scripts.
- Shared `_register_and_respond` helper factors the try/except/metadata/provider-resolution/payload scaffolding out of every register mutation (specific + unified).
- All registration mutations, the unified register, and `unregisterCapabilityMcp` listed in `_CONFIG_MUTATIONS`; the daemon clears the partition MCP configuration cache after every successful call.

### Phase 3: Invocation Adapter ✓ Complete (`55f04c5`)

- `invokeCapabilityMcpTool` and `testCapabilityMcpTool` shipped.
- Invocation adapter lives in `handlers/capability_mcp_invoke.py` and dispatches via `execute_tool_function()`; the existing `execute_decorator()` handles the `MCPFunctionCall` audit lifecycle end-to-end.
- Return shape matches `CapabilityMcpInvocation` (§6.3): response payload, duration, status, and `_capability_context` for `traceId`/`agentId` carry-through.

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

### Verified in `55f04c5`

- ✓ Remote MCP server registers through `registerCapabilityRemoteMcp` and its tools appear in compatibility queries. (1 REMOTE provider live.)
- ✓ Custom MCP package (S3 presign or Base64) registers through the S3/Base64 mutations and its tools appear in compatibility queries.
- ✓ Git-installed custom MCP package registers through `registerCapabilityCustomMcpGitPackage`, refreshes against a new commit, and reports its installed-vs-upstream version. (5 CUSTOM providers live, including 2 git-installed with commit-scoped endpoints; 38 tools for one of them.)
- ✓ Built-in classification: a module listed in `BUILTIN_MCP_MODULES` renders as `BUILTIN` in the compatibility view without any storage marker or mutation.
- ✓ `invokeCapabilityMcpTool` / `testCapabilityMcpTool` execute a daemon-managed tool and return a compatibility-shaped invocation record; `MCPFunctionCall` rows land as expected via the existing audit decorator.
- ✓ Header keys (`Part-Id`, `x-api-key`, etc.) reach the upstream HTTP request verbatim — no snake-case mangling. (Regression covered by commit `bc0b3d4` in the underlying read path.)
- ✓ Provider deregister: `unregisterCapabilityMcp(moduleName, expectedTransport?, updatedBy)` removes function/module/orphaned-setting rows for the module and refuses the delete when the source doesn't match the caller's `expectedTransport` guard.
- ✓ Unified registration: `registerCapabilityMcp(transport, moduleName, …)` dispatches to the same underlying handlers as the specific `registerCapability*` mutations, returns the same `CapabilityMcpRegistrationPayload`, and surfaces clear runtime errors for missing transport-specific required arguments and for feature-flag gates (`enable_s3_package_upload`).
- ✓ Existing daemon mutations and queries continue to work unchanged.
- ✓ No daemon foundation backend schema or dispatcher core changes were needed.

### Pending Phase 4/5

- Banyanos capability engine (or gateway callers) rewired to call the daemon's compatibility layer for MCP management, with historical `tenant_capability_mcp_*` tables frozen for writes.
- Legacy capability engine MCP transport package/runtime removed after production traffic and data reads are verified against daemon outputs.
- Unit/integration tests for the compatibility resolvers (currently none).

## 13. Reference Map

Shipped artifacts (commit `55f04c5`):

| File | Purpose |
| ---- | ------- |
| `mcp_daemon_engine/types/capability_mcp.py` | `CapabilityMcpProvider`, `CapabilityMcpTool`, `CapabilityMcpInvocation`, their connection wrappers, `CapabilityMcpTransport` enum, `CapabilityMcpRegistrationPayload`, `CapabilityMcpGitVersionInfo`, `CapabilityMcpUnregistrationPayload`. |
| `mcp_daemon_engine/queries/capability_mcp.py` | 7 resolvers: `resolve_capability_mcp_provider`, `_provider_list`, `_server`, `_server_tools`, `_tool`, `_tool_list`, `_invocation_list`. |
| `mcp_daemon_engine/mutations/capability_mcp.py` | 11 mutations: remote, custom-S3 (+ upload URL), custom-Base64, custom-Git (+ refresh + version check), invoke, test, unified register, unified unregister. Plus shared helpers `_register_and_respond` (register scaffolding factored out of every specific + unified register mutation), `_unregister_module`, `_resolve_expected_sources`, `_module_setting_id`, `_setting_still_referenced`. |
| `mcp_daemon_engine/handlers/capability_mcp_invoke.py` | Invocation adapter — dispatches via `execute_tool_function()` and normalizes the return shape. |
| `mcp_daemon_engine/schema.py` | Registers all 18 new fields on `Query`/`Mutation` (7 queries + 11 mutations) and adds the 8 new types (incl. `CapabilityMcpUnregistrationPayload`) to `type_class()`. |
| `mcp_daemon_engine/main.py` | Adds the 18 compatibility actions to the `deploy()` manifest and extends `_CONFIG_MUTATIONS` so every registration, deregistration, and unified-register call triggers automatic cache-clear. |
| `mcp_daemon_engine/handlers/config.py` | Parses `BUILTIN_MCP_MODULES` env var (comma-separated) or `builtin_modules` settings key into `Config.setting["builtin_modules"]`. |

Underlying daemon primitives leveraged (unchanged):

- `handlers/mcp_external.py:sync_external_mcp_server` — remote MCP inventory sync.
- `handlers/mcp_handlers.py:{generate_upload_url, process_mcp_package, process_base64_package, load_mcp_configuration_into_models, validate_manifest}` — package upload + manifest loading.
- `handlers/mcp_git.py:{install_mcp_package_from_git, refresh_mcp_git_package, check_mcp_git_package_version}` — Git install/refresh/version-check.
- `handlers/mcp_utility.py:{execute_tool_function, execute_decorator, get_mcp_configuration_with_retry}` — runtime dispatch + audit.
- `handlers/config.py:{fetch_mcp_configuration, _fetch_modules_and_settings, clear_mcp_configuration_cache}` — module/setting read path (with `bc0b3d4` header-key preservation).
