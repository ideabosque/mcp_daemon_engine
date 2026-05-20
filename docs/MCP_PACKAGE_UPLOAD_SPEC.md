# MCP Package Upload and Configuration Load

> Status: Implemented
> Document version: 5.3
> Last updated: 2026-05-19

## 1. Overview

The daemon supports a GraphQL-driven workflow for loading MCP Python packages:

1. Generate a short-lived S3 presigned PUT URL.
2. Upload the ZIP directly to S3.
3. Call `processMcpPackage` to download, validate, stage, and load the package manifest into DynamoDB.

For small development packages, `loadMcpConfiguration` also supports a Base64 inline ZIP shortcut through `packageBase64`.

Both flows use the same Graphene schema path in FastAPI SSE mode and SilvaEngine Lambda mode: `Config.mcp_core.mcp_core_graphql()`.

## 2. Implemented Files

| File | Responsibility |
| ---- | -------------- |
| `mcp_daemon_engine/mutations/mcp_upload.py` | GraphQL mutations for upload URL generation and package processing. |
| `mcp_daemon_engine/types/mcp_configuration_stats.py` | Shared stats payload type. |
| `mcp_daemon_engine/handlers/mcp_handlers.py` | Upload URL generation, ZIP validation, manifest validation, Base64 handling, package processing, and model loading. |
| `mcp_daemon_engine/mutations/mcp_configuration.py` | `loadMcpConfiguration`, including the Base64 package path. |
| `mcp_daemon_engine/handlers/schema.py` | Registers upload mutations and stats type. |
| `mcp_daemon_engine/main.py` | Registers upload/config/module/setting mutations in the SilvaEngine deploy manifest. |

## 3. Runtime Contract

The runtime loader in `handlers/mcp_utility.py` expects packages to follow the canonical S3 key convention:

```text
s3://{Config.funct_bucket_name}/{package_name}.zip
```

At execution time, `_download_and_extract_package(package_name)` downloads that ZIP into `Config.funct_zip_path`, extracts it into `Config.funct_extract_path`, appends `Config.funct_extract_path` to `sys.path`, and imports `module_name`.

Uploaded packages should therefore expose one importable root:

```text
{module_name}/__init__.py
```

Single-file modules (`{module_name}.py`) are accepted by upload validation, but the directory form is preferred because `_module_exists()` checks for an extracted directory.

The runtime S3 download path is gated by the `source` field on each `MCPModule`. `_get_module()` only triggers `_download_and_extract_package()` when `source` is truthy. Modules persisted with `source=""` are expected to be importable directly from the daemon's `sys.path` and will never trigger an S3 fetch at runtime. `processMcpPackage` writes `source="s3"` by default, which is what the runtime contract expects.

## 4. GraphQL API

### 4.1 `generateMcpPackageUploadUrl`

```graphql
type GenerateMcpPackageUploadUrlPayload {
    ok: Boolean!
    message: String
    uploadUrl: String
    s3Key: String
    expiresAt: DateTime
}

extend type Mutation {
    generateMcpPackageUploadUrl(packageName: String!): GenerateMcpPackageUploadUrlPayload
}
```

Behavior:

- Validates `packageName` against `^[A-Za-z][A-Za-z0-9_]*$`.
- Raises `ok: false` if `Config.funct_bucket_name` is unset (i.e. `FUNCT_BUCKET_NAME` is missing from the daemon configuration).
- Generates a presigned S3 `put_object` URL using `Config.aws_s3`.
- Uses canonical key `{packageName}.zip`.
- Sets `ContentType=application/zip` on the presigned params; the client PUT must send the matching `Content-Type` header.
- Uses a 900-second (15-minute) expiration. `expires_at` is returned as `pendulum.now("UTC") + 15 minutes`.

### 4.2 `processMcpPackage`

```graphql
type McpConfigurationStats {
    tools: Int
    resources: Int
    prompts: Int
    modules: Int
    settings: Int
}

type ProcessMcpPackagePayload {
    ok: Boolean!
    message: String
    stats: McpConfigurationStats
}

extend type Mutation {
    processMcpPackage(
        s3Key: String!
        moduleName: String!
        packageName: String!
        source: String
        variables: JSONCamelCase
        updatedBy: String!
    ): ProcessMcpPackagePayload
}
```

Processing sequence (as implemented in `process_mcp_package()` and its helper `download_and_validate_zip()`):

1. Validate `packageName` and `moduleName` against the name policy.
2. Require `s3Key` to equal `{packageName}.zip`; otherwise raise.
3. Download the object from `Config.funct_bucket_name` into a fresh temp directory (`tempfile.mkdtemp(prefix="mcp_upload_")`).
4. Verify the downloaded file is a valid ZIP via `zipfile.is_zipfile`.
5. Reject ZIP members with absolute paths, Windows drive prefixes (`:` in the first segment), or `..` traversal — checked **before** extraction.
6. Extract into the temp validation directory.
7. Load `mcp_configuration.json` from the archive root; fail if missing or invalid JSON.
8. Run `validate_manifest()` (see §7) against the parsed dict.
9. Verify the archive exposes either `{moduleName}/` (directory) or `{moduleName}.py` (single file).
10. Copy the temp ZIP to `Config.funct_zip_path/{packageName}.zip`.
11. Extract the canonical ZIP into `Config.funct_extract_path` so subsequent in-process imports resolve locally.
12. **Clear** the MCP configuration cache for the current `partition_key`.
13. Persist the parsed manifest through `load_mcp_configuration_into_models()` (passing `mcp_configuration`, `module_name`, `package_name`, `source` defaulting to `"s3"`, `variables`, and `updated_by`).
14. **Warm** the cache with `Config.fetch_mcp_configuration(partition_key, force_refresh=True)`. Failure here is logged as a warning but does not change the mutation's `ok=true` result.
15. Remove the temp directory regardless of outcome.

The cache is cleared *before* DB writes and warmed *after*, so any in-flight readers see consistent state.

## 5. `loadMcpConfiguration`

```graphql
type LoadMcpConfigurationPayload {
    ok: Boolean
    message: String
    stats: McpConfigurationStats
}

extend type Mutation {
    loadMcpConfiguration(
        packageBase64: String
        packageName: String
        moduleName: String
        source: String
        mcpConfiguration: JSONCamelCase
        variables: JSONCamelCase
        updatedBy: String!
    ): LoadMcpConfigurationPayload
}
```

`LoadMcpConfiguration.mutate()` dispatches in this order:

1. **`packageBase64` present** → `process_base64_package()`: decode the Base64 string, upload bytes to `s3://{Config.funct_bucket_name}/{packageName}.zip` via `upload_fileobj`, then delegate to `process_mcp_package()`. This branch goes through the full validate/stage/persist/cache-refresh flow.
2. **`mcpConfiguration` present** → `load_mcp_configuration_into_models()` is called directly with the inline manifest. **No** ZIP validation, **no** cache invalidation.
3. **`moduleName` present** → `load_mcp_configuration_into_models()` imports the module and reads `module.MCP_CONFIGURATION`. **No** ZIP validation, **no** cache invalidation.
4. **None of the above** → returns `ok: false`.

Notable consequences of (2) and (3):

- Cache staleness: callers that update configuration via the inline or import path must call `Config.clear_mcp_configuration_cache(partition_key)` themselves if they need readers to see the change immediately. Only the `packageBase64` and `processMcpPackage` paths refresh the cache automatically.
- The inline `mcpConfiguration` path does not run `validate_manifest()`. Cross-reference rules are only enforced when the manifest comes from a ZIP.

Base64 is intended for small dev/test packages. API Gateway has a 10 MB request limit, and Base64 adds about 33 percent overhead. There is currently no daemon-side size cap on `packageBase64`; transport limits are the only gate.

All three success paths now populate `stats` on the payload using `McpConfigurationStats`.

## 6. Package Layout

Recommended ZIP structure:

```text
weather_tools.zip
|-- mcp_configuration.json
|-- weather_tools/
|   |-- __init__.py
|   |-- weather_tool.py
|   `-- helpers.py
`-- requirements.txt
```

`requirements.txt` is informational only. The daemon does not install dependencies during upload processing or runtime execution.

## 7. Manifest Shape

```json
{
  "tools": [
    {
      "name": "weather_lookup",
      "description": "Look up weather by city",
      "inputSchema": {
        "type": "object",
        "properties": {
          "city": { "type": "string" }
        },
        "required": ["city"]
      },
      "is_async": false
    }
  ],
  "resources": [],
  "prompts": [],
  "module_links": [
    {
      "type": "tool",
      "name": "weather_lookup",
      "module_name": "weather_tools",
      "class_name": "WeatherTool",
      "function_name": "get_weather",
      "return_type": "text",
      "is_async": false
    }
  ],
  "modules": [
    {
      "module_name": "weather_tools",
      "package_name": "weather_tools",
      "class_name": "WeatherTool",
      "setting": {
        "api_key": "",
        "base_url": "https://api.example.com"
      },
      "source": "s3"
    }
  ]
}
```

Validation rules (enforced by `validate_manifest()` in `handlers/mcp_handlers.py`):

| Rule | Requirement |
| ---- | ----------- |
| 1 | Manifest must be a JSON object (`dict`). |
| 2 | At least one `tools`, `resources`, or `prompts` entry must have a `name`. |
| 3 | `tools`, `resources`, and `prompts`, when present, must be lists. |
| 4 | Every `module_links[*].name` must reference a declared function. |
| 5 | Every `module_links[*].type` must match the referenced function type. |
| 6 | Every module link must include `module_name`, `class_name`, and `function_name`. |
| 7 | Every `modules[*].module_name` must be non-empty. |
| 8 | Every `modules[*].class_name` must be non-empty. |
| 9 | If any `module_links` are declared, every `modules[*].module_name` must be referenced by at least one link. |
| 10 | When the caller passes a `module_name` argument (the `processMcpPackage` path always does), every manifest module name must equal it. |

The manifest is a Python-consumed JSON dictionary, so internal keys should use snake_case: `module_links`, `module_name`, `class_name`, `function_name`, `return_type`, and `is_async`.

## 8. Persistence Behavior

`load_mcp_configuration_into_models()` writes:

- `tools`, `resources`, and `prompts` into `mcp-functions`.
- `module_links` into the module-related fields on `mcp-functions`.
- A shared merged setting row into `mcp-settings`.
- Module rows into `mcp-modules`.

Settings merge order (each step only **overrides** keys that already exist; new keys cannot be added at higher precedence):

1. Union of every `modules[*].setting` dict in the manifest. This is the universe of allowed keys.
2. Matching keys from `Config.setting` (daemon-level defaults supplied at startup).
3. Matching keys from request `variables` (per-mutation overrides).

If a key needs to exist as an override, the manifest module must declare it (typically with an empty default value).

The loader prefers an explicit `mcp_configuration` dictionary before falling back to importing `module.MCP_CONFIGURATION`. That keeps `processMcpPackage` faithful to the ZIP manifest it validated, even though it still passes `module_name`/`package_name` through to the loader.

## 9. Error Handling

| Failure | Result |
| ------- | ------ |
| Invalid package/module name | `ok: false`; no S3 or DB writes. |
| Presign failure | `ok: false`; no side effects. |
| Client upload failure | Client receives S3 error; daemon is not involved. |
| Missing S3 object | `ok: false`; no DB writes. |
| Invalid or unsafe ZIP | `ok: false`; no DB writes. |
| Missing or invalid manifest | `ok: false`; no DB writes. |
| Manifest cross-reference failure | `ok: false`; no DB writes. |
| DB write failure | `ok: false`; partial idempotent upserts may exist and can be retried. |
| Cache warm failure after DB writes | Package load still succeeds; warning is logged. |

## 10. Security Notes

- Upload URLs are scoped to one S3 object key and PUT only.
- Package and module names are restricted to Python-identifier-like names.
- ZIP extraction rejects absolute paths, Windows drive prefixes, and `..` traversal.
- Uploaded code executes in the daemon process. Only trusted administrators should be allowed to call upload and process mutations.
- The SilvaEngine deploy manifest still marks `mcp_core_graphql` as `is_auth_required: False`; production deployments should ensure transport-level or resolver-level admin authorization.

## 11. Test Coverage Targets

The repo has no formal test runner. `mcp_daemon_engine/tests/test_mcp_daemon_engine.py` currently exercises `loadMcpConfiguration` via the `moduleName` import path (`test_graphql_load_mcp_configuration`); the new `processMcpPackage` and `generateMcpPackageUploadUrl` mutations have no direct tests yet.

The highest-value tests to add first are helper-level tests around:

- `_validate_package_name` accepting/rejecting names against the regex.
- `validate_manifest`:
  - Rules 4–6 (module-link references and required keys).
  - Rules 7–8 (module name and class name non-empty).
  - Rules 9–10 (link/module name cross-referencing and the `module_name` argument match).
- `download_and_validate_zip`:
  - Mismatched `s3_key` vs. `{packageName}.zip` raises before download.
  - ZIP path traversal rejection (absolute paths, Windows drive prefixes, `..`).
  - Missing `mcp_configuration.json` raises.
  - Missing `{moduleName}/__init__.py` or `{moduleName}.py` raises.
- `process_mcp_package` cache-clear-then-warm ordering around `load_mcp_configuration_into_models()`.
- `process_base64_package` decode → upload → delegate to `process_mcp_package`.
- `LoadMcpConfiguration` dispatch (Base64 vs inline `mcpConfiguration` vs `moduleName` vs none).
- GraphQL schema registration for `generateMcpPackageUploadUrl`, `processMcpPackage`, and `loadMcpConfiguration` (smoke check via `Graphql.generate_graphql_operation`).

AWS calls should be mocked (`moto` for S3, or `botocore.stub.Stubber`) to keep tests hermetic.
