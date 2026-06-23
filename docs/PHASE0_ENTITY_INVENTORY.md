# Phase 0: Entity Field Inventory

> Per-field DynamoDB→PostgreSQL type mapping for the 4 metadata entities.
> S3, MCP runtime, external proxy, and dynamic modules are NOT backend-selectable.
> Last reviewed: 2026-06-22

## S3 Decision (confirmed)

In PostgreSQL mode, `aws_s3` is initialized **unconditionally** when `funct_bucket_name` is set,
regardless of whether AWS credentials are present. This diverges from `rfq_engine`'s "AWS fully
optional in PG mode" rule because `mcp_daemon_engine` uses S3 for:
1. MCP package upload/download (mandatory runtime feature).
2. Large-content offload for `mcp_function_call` (capability preserved across backends).

When credentials are absent, S3 falls back to the default credential chain.

## Entity: MCPFunction (table: `mcp-functions` / `mcp_functions`)

| Field | DynamoDB type | PostgreSQL column | Notes |
| --- | --- | --- | --- |
| `partition_key` | `UnicodeAttribute` (hash) | `String(128)`, PK | Tenant key |
| `name` | `UnicodeAttribute` (range) | `String`, PK | Natural key |
| `mcp_type` | `UnicodeAttribute` | `String` | tool/resource/prompt; LSI range |
| `description` | `UnicodeAttribute(null=True)` | `Text` | |
| `data` | `MapAttribute()` | `JSONB` | inputSchema, etc. |
| `annotations` | `UnicodeAttribute(null=True)` | `Text` | JSON string today |
| `module_name` | `UnicodeAttribute(null=True)` | `String` | |
| `class_name` | `UnicodeAttribute(null=True)` | `String` | |
| `function_name` | `UnicodeAttribute(null=True)` | `String` | |
| `return_type` | `UnicodeAttribute(null=True)` | `String` | |
| `is_async` | `BooleanAttribute(null=True)` | `Boolean` | |
| `updated_by` | `UnicodeAttribute()` | `String(64)` | |
| `created_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |
| `updated_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |

**Indexes:**
- DynamoDB LSI: `mcp_type_index` (range `mcp_type`).
- PostgreSQL: `Index("idx_mcp_functions_partition_mcp_type", "partition_key", "mcp_type")`.

## Entity: MCPModule (table: `mcp-modules` / `mcp_modules`)

| Field | DynamoDB type | PostgreSQL column | Notes |
| --- | --- | --- | --- |
| `partition_key` | `UnicodeAttribute` (hash) | `String(128)`, PK | |
| `module_name` | `UnicodeAttribute` (range) | `String`, PK | |
| `package_name` | `UnicodeAttribute()` | `String` | LSI range |
| `classes` | `ListAttribute(of=MapAttribute)` | `JSONB` | list of {class_name, setting_id} |
| `source` | `UnicodeAttribute(null=True)` | `String` | "s3" / "external" / "" |
| `updated_by` | `UnicodeAttribute()` | `String(64)` | |
| `created_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |
| `updated_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |

**Indexes:**
- DynamoDB LSI: `mcp_package_index` (range `package_name`).
- PostgreSQL: `Index("idx_mcp_modules_partition_package_name", "partition_key", "package_name")`.

**Special behavior:** `purge_cache()` cascades into `mcp_setting` by walking `classes` for `setting_id`s.

## Entity: MCPSetting (table: `mcp-settings` / `mcp_settings`)

| Field | DynamoDB type | PostgreSQL column | Notes |
| --- | --- | --- | --- |
| `partition_key` | `UnicodeAttribute` (hash) | `String(128)`, PK | |
| `setting_id` | `UnicodeAttribute` (range) | `String`, PK | |
| `setting` | `MapAttribute()` | `JSONB` | config blob |
| `updated_by` | `UnicodeAttribute()` | `String(64)` | |
| `created_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |
| `updated_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |

**Indexes:** None (no secondary index in DynamoDB or PostgreSQL).

## Entity: MCPFunctionCall (table: `mcp-function_calls` / `mcp_function_calls`)

| Field | DynamoDB type | PostgreSQL column | Notes |
| --- | --- | --- | --- |
| `partition_key` | `UnicodeAttribute` (hash) | `String(128)`, PK | |
| `mcp_function_call_uuid` | `UnicodeAttribute` (range) | `String`, PK | `uuid.uuid4()` string, not UUID-typed |
| `name` | `UnicodeAttribute()` | `String` | LSI range |
| `mcp_type` | `UnicodeAttribute()` | `String` | LSI range |
| `arguments` | `MapAttribute()` | `JSONB` | |
| `content_in_s3` | `BooleanAttribute(default=False)` | `Boolean` | |
| `content` | `UnicodeAttribute(null=True)` | `Text` | may be S3-offloaded |
| `status` | `UnicodeAttribute(default="initial")` | `String` | |
| `notes` | `UnicodeAttribute(null=True)` | `Text` | |
| `time_spent` | `NumberAttribute(null=True)` | `Integer` | |
| `updated_by` | `UnicodeAttribute()` | `String(64)` | |
| `created_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | |
| `updated_at` | `UTCDateTimeAttribute()` | `TIMESTAMP(timezone=True)` | LSI range (string-typed in DD B) |

**Indexes:**
- DynamoDB LSIs: `mcp_type_index` (range `mcp_type`), `name_index` (range `name`), `updated_at_index` (range `updated_at`).
- PostgreSQL: `Index("idx_mcp_function_calls_partition_mcp_type", "partition_key", "mcp_type")`, `Index("idx_mcp_function_calls_partition_name", "partition_key", "name")`, `Index("idx_mcp_function_calls_partition_updated_at", "partition_key", "updated_at")`.

**Special behaviors:**
1. **S3 content offload:** DynamoDB auto-offloads when item exceeds 400KB. PostgreSQL has no such limit — PG `insert_update` only offloads when caller explicitly sets `content_in_s3=True`. PG `get`/`list` must still hydrate `content` from S3 when `content_in_s3` is set.
2. **Newest-first ordering:** `resolve_mcp_function_call_list` uses `scan_index_forward=False`. PG `list()` must use `order_by(updated_at.desc())`.

## No `uuid-ossp` Extension Required

All keys are `String` (not UUID-typed), so PostgreSQL does not need the `uuid-ossp` extension.
The `mcp_function_call_uuid` is generated by `str(uuid.uuid4())` in Python and stored as a string.

## No Single-Active Invariant

Unlike `knowledge_graph_engine`, no entity has an "at most one active record per partition" constraint.
No partial unique indexes are required.