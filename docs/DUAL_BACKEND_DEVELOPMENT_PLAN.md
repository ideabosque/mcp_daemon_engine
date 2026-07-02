# MCP Daemon Engine Dual-Backend Development Plan

> Project: `mcp_daemon_engine`
> Goal: support DynamoDB and PostgreSQL as deployment-selectable persistence backends for the engine's four metadata entities, behind a single GraphQL contract.
> Scope boundary: **S3 (package storage + large-content offload), the MCP SSE/stdio runtime, the external-MCP proxy, and dynamically-loaded tool modules are out of scope for backend selection.** They stay active under both `DB_BACKEND` values. The dual backend applies only to the four PynamoDB metadata models (`MCPFunction`, `MCPModule`, `MCPSetting`, `MCPFunctionCall`).
> Status: **Phases 0–3 complete; Phase 6 partial; Phases 4–5 pending.** The repository dispatch boundary is implemented and enforced by a static adoption guard — all GraphQL queries, mutations, and the configuration-loading handler route persistence through `get_repo()`. DynamoDB is the default and is dispatch-verified. PostgreSQL is structurally complete (models, repositories, migrations, cache-purge parity, S3-offload divergence) for all 4 entities, but has **not** yet been validated against a running PostgreSQL service. The backend-agnostic dispatch test covers only the DynamoDB arm today; the PostgreSQL arm and repository CRUD tests are not yet written.
> No backward support: the daemon engine is not yet in production with persisted data, so this plan carries **no backward-compatibility or data-migration obligations.** Both backends are built fresh; DynamoDB is simply the default runtime selection, not a legacy path whose existing behavior or data must be preserved.
> Last reviewed: 2026-06-22
> Verified against source on: 2026-06-22

## Executive Summary

`mcp_daemon_engine` persists one class of data:

- **Metadata** — four models built on `silvaengine_dynamodb_base.BaseModel`: MCP functions (tools/resources/prompts), MCP modules (package + class registry), MCP settings (per-class configuration blobs), and MCP function calls (execution audit records, with large content optionally offloaded to S3). This is the layer that is backend-selectable.

Everything else the daemon does is **not** part of the DynamoDB↔PostgreSQL swap:

- **S3** — presigned upload URLs for MCP packages, downloaded/extracted zip packages, and offloaded `mcp_function_call` content that exceeds DynamoDB's 400KB item limit. Reached through `Config.aws_s3` and `Config.funct_bucket_name`.
- **The MCP runtime** — SSE transport, stdio transport, the MCP JSON-RPC protocol handlers, the SSE client registry, and the GraphQL proxy loopback. Reached through `handlers/mcp_server.py`, `handlers/mcp_app.py`, `handlers/sse_manager.py`.
- **Dynamically loaded tool modules** — zip packages downloaded from S3, extracted to `/tmp/functs`, appended to `sys.path`, and instantiated by class name. Reached through `handlers/mcp_utility.py`.
- **External MCP proxy** — Phase 2 proxy execution that forwards calls to a remote MCP HTTP server via `MCPHttpClient`. Reached through `handlers/external_mcp_proxy.py` / `handlers/mcp_external.py`.

The codebase now runs on a dual-backend structure:

- `DB_BACKEND=dynamodb` (default): PynamoDB models under `mcp_daemon_engine.models.dynamodb`, DynamoDB DataLoaders, existing `@method_cache` / cache-decorator behavior, and DynamoDB table initialization.
- `DB_BACKEND=postgresql`: SQLAlchemy models under `mcp_daemon_engine.models.postgresql`, 4 Alembic migrations, 4 PostgreSQL repositories under `mcp_daemon_engine.models.repositories.postgresql`, and a SQLAlchemy `scoped_session` (`Config.db_session`).

The repository boundary lives in `mcp_daemon_engine.models.repositories`. It isolates GraphQL queries, mutations, and the configuration-loading handler from backend-specific persistence details. **All metadata persistence calls now flow through this boundary** (`get_repo(entity_type)` for query/mutation operations; `get_loaders(context)` is a forward-compatible stub since no nested resolvers exist today). The PostgreSQL path should be treated as **implementation-ready and validation-incomplete** rather than production-ready: the wiring is real and compile-clean, but no GraphQL query, mutation, or repository method has yet been executed against a live PostgreSQL database.

> Note on `handlers/mcp_utility.py`: `_insert_update_mcp_function_call` routes through the internal GraphQL loopback (`_dispatch_internal_graphql`), not through direct `models.*` imports. This makes it backend-agnostic by construction — the boundary adoption did not need to touch it, and it must not be regressed to direct model imports.

## Current Architecture

```text
GraphQL schema, queries, mutations, handlers/mcp_handlers.py
        |  (all metadata persistence routes through the dispatch boundary)
        v
mcp_daemon_engine.models.repositories
   dispatch.get_repo(entity_type)        -> active repository
   dispatch.get_loaders(context)         -> stub (returns None; no nested resolvers)
        |
        +-- DynamoDB implementation
        |      mcp_daemon_engine.models.dynamodb
        |      4 PynamoDB entity modules, cache.py, utils.py
        |      mcp_daemon_engine.models.repositories.dynamodb  (4 thin wrappers + _base.py)
        |
        +-- PostgreSQL implementation
               mcp_daemon_engine.models.postgresql
               4 SQLAlchemy entity modules, base.py, utils.py
               mcp_daemon_engine.models.repositories.postgresql  (4 repository classes)
               migration/alembic  (repo-root; 4 migrations, 0001-0004)

   [unchanged, backend-independent]
   S3 package storage + large-content offload via Config.aws_s3
   MCP runtime (SSE/stdio/JSON-RPC) via handlers/mcp_server.py / mcp_app.py / sse_manager.py
   External MCP proxy via handlers/external_mcp_proxy.py / mcp_external.py
   Dynamically loaded tool modules via handlers/mcp_utility.py
```

Dispatch rules (verified 2026-06-22):

- `Config.DB_BACKEND` selects the active backend at deployment initialization time (`handlers/config.py:105`, `:257`). Only `"dynamodb"` and `"postgresql"` are valid; any other value raises `ValueError` (`config.py:266`).
- `get_repo(entity_type)` lazily registers and returns the active backend repository (`models/repositories/dispatch.py:33-55`). A `KeyError` is raised if no repository is registered for the requested entity on the active backend.
- `get_loaders(context)` is a stub that returns `None` (`dispatch.py:58-64`). No nested resolvers exist today; the symbol is exposed so a future nested-resolver surface can adopt it without touching the dispatch seam.
- GraphQL queries import `get_repo` from `..models.repositories` and call `get_repo("mcp_function").resolve_single(...)` / `.list(...)` (e.g. `queries/mcp_function.py:16-26`). Mutations call `get_repo("mcp_function").insert_update(...)` / `.delete(...)` (e.g. `mutations/mcp_function.py:15-37`).
- `handlers/mcp_handlers.py` (the `load_mcp_configuration_into_models` batch-load flow) imports `get_repo` from `..models.repositories` and calls `get_repo("mcp_function").insert_update(...)`, `get_repo("mcp_module").insert_update(...)`, `get_repo("mcp_setting").insert_update(...)` (`mcp_handlers.py:51, 100, 123, 146, 168, 203, 223`).
- The combination of `@method_cache` on model getters and the `@purge_cache()` decorator on DynamoDB `insert_update_*` / `delete_*` functions handles cache invalidation under DynamoDB. The PG repositories replicate the `purge_entity_cascading_cache` side effect explicitly after each commit.

## Repository Adoption Status (verified 2026-06-22)

A source audit on 2026-06-22 confirmed full adoption of the dispatch boundary by production code:

- **4 of 4** query modules (`queries/mcp_function.py`, `queries/mcp_module.py`, `queries/mcp_setting.py`, `queries/mcp_function_call.py`) import `get_repo` from `..models.repositories` and call `get_repo(entity).resolve_single(...)` / `.list(...)`. No query module imports `models.dynamodb` or calls DynamoDB `resolve_*` / `insert_update_*` / `delete_*` functions directly.
- **4 of 4** mutation modules import `get_repo` from `..models.repositories` and call `get_repo(entity).insert_update(...)` / `.delete(...)`. No mutation calls DynamoDB free functions directly.
- **1 of 1** handler module (`handlers/mcp_handlers.py`) imports `get_repo` for the batch-load flow. No handler imports `models.dynamodb` directly.
- The GraphQL layer and `handlers/mcp_handlers.py` contain **zero** direct `models.dynamodb` imports (verified by `tests/test_dual_backend_guard.py` — a static adoption guard that fails the build if any direct `models.dynamodb` import or direct `insert_update_*` / `delete_*` free-function call reappears in `queries/`, `mutations/`, or `handlers/mcp_handlers.py`).
- `handlers/mcp_utility.py` was **not** modified — it routes function-call audit writes through the internal GraphQL loopback (`_dispatch_internal_graphql`), which is backend-agnostic by construction.

Consequence: setting `DB_BACKEND=postgresql` routes GraphQL persistence through the PostgreSQL repositories. The remaining gating work is "prove runtime parity against a real PostgreSQL database and add the PostgreSQL arm of the backend-agnostic tests."

## Implemented File Layout

```text
mcp_daemon_engine/
  handlers/
    config.py
      Config.DB_BACKEND (default "dynamodb")                  # config.py:105
      Config.db_session (PostgreSQL scoped_session; PG only)   # config.py:108
      _initialize_dynamodb_meta(setting)                       # config.py:382
      _initialize_optional_aws_services(setting)               # config.py:398 (S3 unconditional)
      _initialize_db_session(setting)                          # config.py:460
      _initialize_tables(logger)                               # config.py:488 (backend-dispatched)
      CACHE_ENTITY_CONFIG_DYNAMODB / _POSTGRESQL               # config.py:125, :157
      CACHE_RELATIONSHIPS_DYNAMODB / _POSTGRESQL               # config.py:169, :191
      get_cache_entity_config() / get_cache_relationships()    # config.py:160, :194

  models/
    __init__.py                    # empty
    repositories/
      base.py                       # EntityRepository ABC + RepositoryError family
      dispatch.py                   # get_repo, get_loaders (stub), register_repo, clear_registry, lazy init
      __init__.py                   # re-exports get_repo, get_loaders, register_repo, clear_registry, EntityRepository
      dynamodb/                     # 4 thin DynamoDB repository wrappers + _base.py
        __init__.py                 # register_all (4 entries)
        _base.py                    # _normalize(model) -> normalize_to_json(attribute_values)
        mcp_function_repo.py  mcp_module_repo.py
        mcp_setting_repo.py   mcp_function_call_repo.py
      postgresql/                   # 4 PostgreSQL repository classes
        __init__.py                 # register_all (4 entries; ImportError-swallowing)
        mcp_function_repo.py  mcp_module_repo.py
        mcp_setting_repo.py   mcp_function_call_repo.py

    dynamodb/                       # the 4 PynamoDB modules (moved from models/*.py)
      mcp_function.py  mcp_module.py  mcp_setting.py  mcp_function_call.py
      cache.py                      # cascading cache purger (_extract_module_setting_ids, purge_entity_cascading_cache)
      utils.py                      # initialize_tables(logger) for the 4 tables

    postgresql/                     # only imported when DB_BACKEND=postgresql
      base.py                       # declarative_base() Base, normalize_row, _serialize_value
      utils.py                      # initialize_tables(logger, db_session) -> Base.metadata.create_all(checkfirst=True)
      mcp_function.py  mcp_module.py  mcp_setting.py  mcp_function_call.py

  migration/                        # NOTE: inside the package, not at repo root
    alembic.ini
    alembic/
      env.py                        # DATABASE_URL > Config > alembic.ini fallback; compare_type=True
      versions/
        0001_create_mcp_functions.py
        0002_create_mcp_modules.py
        0003_create_mcp_settings.py
        0004_create_mcp_function_calls.py

  utils/
    normalization.py                # normalize_to_json (shared by both backend repos)
    exceptions.py

  tests/
    test_dual_backend_guard.py     # static adoption guard + DynamoDB dispatch smoke
    test_mcp_daemon_engine.py      # existing DynamoDB+S3 integration tests
    test_mcp_package_import.py      # existing package-import test
```

Notes on what is **not** present:

- No `batch_loaders/` directory under either backend — no nested resolvers exist today. The `get_loaders` stub exists only so the seam is forward-compatible.
- No `models/repositories/utils.py` (rfq_engine's backend-dispatched `combine_all_*` helpers). `mcp_daemon_engine` has no cross-entity combination helpers at the GraphQL layer.
- No `migration/migrate_dynamodb_to_postgresql.py`. No production data to migrate.
- The migration tree lives at the repo root `migration/`, matching the `rfq_engine` / `knowledge_graph_engine` convention. `POSTGRESQL_SETUP.md` documents `alembic -c migration/alembic.ini`. Migrations are no longer shipped inside the installed Python package; production deploys that need them should copy the tree alongside the package or run migrations from a checkout.

## Persisted Entities

The dual-backend structure covers these 4 metadata entities. S3, the MCP runtime, the external proxy, and dynamic modules are **not** in this table — they are separate concerns, not swappable backends.

| Entity | DynamoDB table | PostgreSQL table | Hash / range keys | Secondary access | Notable fields |
| --- | --- | --- | --- | --- | --- |
| MCPFunction | `mcp-functions` | `mcp_functions` | `partition_key`, `name` | LSI `mcp_type` | `data` (map: inputSchema, etc.), `annotations`, `module_name`, `class_name`, `function_name`, `return_type`, `is_async` |
| MCPModule | `mcp-modules` | `mcp_modules` | `partition_key`, `module_name` | LSI `package_name` | `classes` (list of map: `class_name`, `setting_id`), `source` |
| MCPSetting | `mcp-settings` | `mcp_settings` | `partition_key`, `setting_id` | none | `setting` (map) |
| MCPFunctionCall | `mcp-function_calls` | `mcp_function_calls` | `partition_key`, `mcp_function_call_uuid` | LSI `mcp_type`, LSI `name`, LSI `updated_at` | `arguments` (map), `content` (may be S3-offloaded), `content_in_s3`, `status`, `notes`, `time_spent` |

Entity-specific behavior that the PostgreSQL repositories preserve:

- **S3 content offload (divergence documented).** `MCPFunctionCall` may exceed DynamoDB's 400KB item limit. The DynamoDB `insert_update_mcp_function_call` catches the size-limit exception, writes `content` to `s3://<bucket>/mcp_content/<uuid>.json`, sets `content_in_s3=True`, and drops the `content` column from the row. The DynamoDB read path (`get_mcp_function_call_type`) transparently fetches from S3 when `content_in_s3` is set. **PostgreSQL has no 400KB row limit**, so the PG repository does **not** auto-offload on size — it only offloads when the caller explicitly sets `content_in_s3=True` (`models/repositories/postgresql/mcp_function_call_repo.py:158`). The PG `get` / `list` / `get_type` still hydrate `content` from S3 when `content_in_s3` is set (`mcp_function_call_repo.py:69-74, 330-336`). This divergence is documented in the repository docstring and in `docs/DUAL_BACKEND_CONFIG.md`.
- **Cascading cache purge.** Each entity's `purge_cache()` decorator calls `purge_entity_cascading_cache` after a successful write/delete. `MCPModule`'s purge additionally walks `classes` for `setting_id`s and cascades into `mcp_setting` (`models/dynamodb/mcp_module.py`). The PG `MCPModulePGRepository` replicates this: `_purge_cache` walks `classes` via `_extract_module_setting_ids` and purges each `mcp_setting` cache entry (`models/repositories/postgresql/mcp_module_repo.py:220-258`). This side effect fires under both backends.
- **`load_mcp_configuration_into_models` batch writes.** `handlers/mcp_handlers.py` calls `get_repo("mcp_function").insert_update(...)`, `get_repo("mcp_module").insert_update(...)`, `get_repo("mcp_setting").insert_update(...)` in a loop to bulk-load a manifest. The loop body is otherwise unchanged from the pre-refactor version.
- **`mcp_function_call` async-audit path.** `handlers/mcp_utility.py:_insert_update_mcp_function_call` routes through the internal GraphQL loopback, so it is backend-agnostic by construction. It was **not** modified during the port and must not be regressed.
- **No single-active invariant.** Unlike `knowledge_graph_engine`, `mcp_daemon_engine` has no "at most one active record per partition" constraint on any entity. No partial unique index is required.

## Repository Contract

Each repository returns normalized dictionaries or explicit scalar results. PynamoDB and SQLAlchemy instances must not leak above the repository boundary.

```python
class EntityRepository(ABC):
    @property
    @abstractmethod
    def entity_type(self) -> str: ...

    @abstractmethod
    def get(self, **keys) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def count(self, **keys) -> int: ...

    @abstractmethod
    def list(self, info, **filters) -> Any: ...

    @abstractmethod
    def insert_update(self, info, **kwargs) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def delete(self, info, **kwargs) -> bool: ...
```

`models/repositories/base.py` also defines `RepositoryError`, `EntityNotFoundError`, and `DependencyExistsError` (the latter unused for now but available if a delete-guard is ever needed). Beyond the six abstract methods, concrete repositories add two conveniences used by the GraphQL layer:

- `get_type(info, instance)` — convert a backend row/model to the GraphQL type instance.
- `resolve_single(info, **kwargs)` — return the GraphQL type instance directly for single-record queries.

Backend implementation patterns (as implemented):

- **DynamoDB repos are thin wrappers.** Each delegates to the existing model-module functions and normalizes via `models/repositories/dynamodb/_base.py::_normalize(model)` → `normalize_to_json(model.attribute_values)`. The DynamoDB `insert_update`/`delete` functions are decorated with `@insert_update_decorator` / `@delete_decorator` / `@purge_cache()` — those decorators stay on the model-layer functions so the cache purge fires under DynamoDB. The repo wrapper calls them as-is.
- **PostgreSQL repos are full SQLAlchemy implementations.** They use `Config.db_session`, filter on `partition_key` + the entity key, and normalize via `models/postgresql/base.py::normalize_row(row)`. Writes follow `try: … session.commit(); session.refresh(row) … except: session.rollback(); raise`. Each PG repo calls `purge_entity_cascading_cache` after a successful commit (replicating the DynamoDB `@purge_cache()` side effect).
- **List translation.** The DynamoDB `resolve_list_decorator` returns `(inquiry_funct, count_funct, args)` and the decorator builds the `*ListType(<entity>_list=[...], page_size=limit, page_number=page_number, total=N)` connection shape (all three fields from `ListObjectType`). The PostgreSQL `list()` builds the same `*ListType` manually: `query.count()` for `total`, `offset/limit` pagination, `order_by(...)` (newest-first for `mcp_function_call` via `updated_at.desc()`). Each entity's `ListType` field name is matched exactly:
  - `MCPFunctionListType.mcp_function_list`
  - `MCPModuleListType.mcp_module_list`
  - `MCPSettingListType.mcp_setting_list`
  - `MCPFunctionCallListType.mcp_function_call_list`
  - **Parity gap (open):** the PG repos currently set only `total` and omit `page_size` / `page_number`, so the connection shape is not yet identical to the DynamoDB `resolve_list_decorator` output. See "Major Risks" and "Immediate Next Work."

Entity-specific helpers (no `rfq_engine` equivalent — these are `mcp_daemon_engine` additions):

- `mcp_function_call`: S3 content hydration on `get`/`list`/`get_type` when `content_in_s3` is set; optional offload on `insert_update` only when the caller explicitly requests it.
- `mcp_module`: cascading `mcp_setting` cache purge after `insert_update`/`delete` (walk `classes` for `setting_id`s).
- `mcp_function`, `mcp_setting`: standard CRUD/list; no special invariants. **Parity gap (open):** the GraphQL schema defines `desc=String(name="description")`, so the resolver kwarg key is `desc`. The DynamoDB `resolve_mcp_function_list` reads `kwargs.get("desc")` (correct). The PG `MCPFunctionPGRepository.list` reads `filters.get("description")` (wrong — always `None`), so the `description` filter is silently broken under PostgreSQL. The PG repo must read `filters.get("desc")` to match. See "Major Risks" and "Immediate Next Work."

## Configuration Contract

`Config.initialize(logger, setting)` owns backend selection and service initialization:

- `setting["db_backend"]` defaults to `"dynamodb"` (`config.py:105`, `:257`); validated against `{"dynamodb", "postgresql"}` (else `ValueError`, `config.py:266`).
- DynamoDB mode initializes AWS clients (`_initialize_aws_services`) and PynamoDB `BaseModel.Meta` credentials (`_initialize_dynamodb_meta`, `:382`). The `BaseModel.Meta` setup was moved here from `MCPDaemonEngine.__init__` during Phase 1 so it is owned by the config singleton and runs for both gateway and standalone-SSE deployments.
- PostgreSQL mode initializes SQLAlchemy `scoped_session` (`_initialize_db_session`, `:460` with `pool_recycle=7200`, `pool_size=10`, `pool_pre_ping=True`, `echo=False`) and only initializes non-S3 AWS clients when credentials are present (`_initialize_optional_aws_services`, `:398`). **S3 stays unconditional in PG mode when `funct_bucket_name` is set** (`:427`) — this is the one place `mcp_daemon_engine` diverges from `rfq_engine`'s "AWS fully optional in PG mode" rule, because the daemon needs S3 for package uploads and content offload even in PG mode. When credentials are absent, S3 falls back to the default credential chain.
- `initialize_tables` delegates to DynamoDB or PostgreSQL table initialization based on `Config.DB_BACKEND` (`:489`); PostgreSQL path uses `Base.metadata.create_all(checkfirst=True)`.
- PostgreSQL dependencies are optional through `mcp-daemon-engine[postgresql]`, which pulls `SQLAlchemy>=1.4`, `psycopg2-binary>=2.9`, and `alembic>=1.10`. These are not in the core dependency list, so DynamoDB-only installs do not require them.

Cache config is backend-aware:

- `CACHE_ENTITY_CONFIG_DYNAMODB` (`:125`) lists all 4 entities with `@method_cache` getters and module paths pointing at `mcp_daemon_engine.models.dynamodb.*`. `CACHE_ENTITY_CONFIG_POSTGRESQL` (`:157`) is intentionally empty because PG repositories do not use `@method_cache`.
- `CACHE_RELATIONSHIPS_DYNAMODB` (`:169`) maps `mcp_module` → [`mcp_function`] and `mcp_function` → [`mcp_function_call`]. `CACHE_RELATIONSHIPS_POSTGRESQL` (`:191`) is empty.
- `get_cache_entity_config()` (`:160`) and `get_cache_relationships()` (`:194`) return the active backend's config automatically. The PG repositories still call `purge_entity_cascading_cache` after writes (the purger resolves resolvers from the active backend's cache config, which is empty for PG, so it is effectively a no-op until PG opts in).

## PostgreSQL Schema Principles

The PostgreSQL schema is not a one-for-one DynamoDB key copy. Principles:

- Preserve tenant ownership with `partition_key` on every table (`<endpoint_id>#<Part-Id>` from the gateway).
- Use `String` columns for the natural range keys (`name`, `module_name`, `setting_id`, `mcp_function_call_uuid`) — `mcp_daemon_engine` does **not** use real UUIDs for these keys (function-call UUIDs are `uuid.uuid4()` strings, but stored as `UnicodeAttribute`, not typed UUIDs). `String` preserves the existing string semantics and avoids a `uuid-ossp` extension dependency.
- Use JSONB for flexible PynamoDB map/list shapes: `MCPFunction.data`, `MCPModule.classes` (list of map), `MCPSetting.setting` (map), `MCPFunctionCall.arguments` (map). `MCPFunction.annotations` is kept as `Text` (JSON string today).
- Use timezone-aware timestamps (`TIMESTAMP(timezone=True)`).
- Use `Text` for potentially large content (`MCPFunction.description`, `MCPFunctionCall.content`, `MCPFunctionCall.notes`).
- Index existing list/filter paths to mirror the DynamoDB LSIs:
  - `mcp_functions`: `(partition_key, mcp_type)`.
  - `mcp_modules`: `(partition_key, package_name)`.
  - `mcp_function_calls`: `(partition_key, mcp_type)`, `(partition_key, name)`, `(partition_key, updated_at)` — `updated_at` is a real `TIMESTAMP` in PG (string-typed LSI range in DynamoDB); PG orders by it natively.

Column-type mapping (per-field detail in `docs/PHASE0_ENTITY_INVENTORY.md`):

| Field | DynamoDB type | PostgreSQL column |
| --- | --- | --- |
| `partition_key` | `UnicodeAttribute` (hash) | `String(128)`, PK part |
| `name` / `module_name` / `setting_id` / `mcp_function_call_uuid` | `UnicodeAttribute` (range) | `String`, PK part |
| `mcp_type` / `source` / `status` / `return_type` / `updated_by` | `UnicodeAttribute` | `String` |
| `description` / `annotations` / `content` / `notes` | `UnicodeAttribute(null)` | `Text` |
| `module_name` / `class_name` / `function_name` (on MCPFunction) | `UnicodeAttribute(null)` | `String` |
| `is_async` / `content_in_s3` | `BooleanAttribute(null/default)` | `Boolean` |
| `time_spent` | `NumberAttribute(null)` | `Integer` |
| `data` (MCPFunction) | `MapAttribute` | `JSONB` |
| `classes` (MCPModule) | `ListAttribute(of=MapAttribute)` | `JSONB` |
| `setting` (MCPSetting) | `MapAttribute` | `JSONB` |
| `arguments` (MCPFunctionCall) | `MapAttribute` | `JSONB` |
| `created_at` / `updated_at` | `UTCDateTimeAttribute` | `TIMESTAMP(timezone=True)` |

- `migration/alembic/env.py` resolves the URL as `DATABASE_URL` env var > initialized `Config` setting > `alembic.ini` fallback, and configures with `compare_type=True`.
- No `uuid-ossp` extension is required because all keys are `String`.

> Implementation note: `migration/alembic/env.py:39` references `Config._initialized`, but `Config` does **not** define an `_initialized` attribute. The surrounding `try/except Exception: pass` swallows the `AttributeError`, so the env.py silently falls through to the `DATABASE_URL` env var or the `alembic.ini` fallback. This is a latent bug — the `Config`-setting URL resolution path never fires. It should be fixed by either adding an `_initialized` class flag to `Config` (set at the end of `initialize()`) or by replacing the guard with a check that actually exists (e.g. `Config.DB_BACKEND` is non-default and `Config.db_session` is not `None`). Track this as a closeout item before relying on env.py's Config-fallback path.

## Phase Status

### Phase 0: Baseline and Contract Inventory — Complete

Completed:

- Captured the 4 metadata entities, their keys, secondary indexes, and special behaviors (S3 offload, cascading cache purge, batch-load handler).
- Documented the scope boundary (S3, MCP runtime, external proxy, dynamic modules are not backend-selectable).
- Documented cache config (all 4 entities covered; two relationship edges; `mcp_module` cascades into `mcp_setting`).
- Written `docs/PHASE0_ENTITY_INVENTORY.md` with per-field DynamoDB→PostgreSQL type mappings.
- Confirmed S3 stays unconditional in PG mode (decision recorded in inventory doc and config doc).

### Phase 1: Backend Dispatch With DynamoDB Pass-Through — Complete

Completed:

- Added `Config.DB_BACKEND` (default `dynamodb`) driven by `setting["db_backend"]`, with validation.
- Moved `BaseModel.Meta` setup from `MCPDaemonEngine.__init__` into `Config._initialize_dynamodb_meta(setting)`.
- Added `models/repositories/{base.py, dispatch.py, __init__.py}` (`get_repo`, `get_loaders` stub, `register_repo`, `clear_registry`, lazy init).
- Moved the 4 PynamoDB modules under `models/dynamodb/` and added 4 thin DynamoDB repository wrappers under `models/repositories/dynamodb/` plus `_base.py` and `register_all`.
- Moved `models/cache.py` under `models/dynamodb/cache.py`.
- Migrated every GraphQL caller to the boundary: `queries/*.py` (4), `mutations/*.py` (4), `handlers/mcp_handlers.py`.
- Split cache config into `CACHE_ENTITY_CONFIG_DYNAMODB` / `CACHE_ENTITY_CONFIG_POSTGRESQL` + backend-aware `get_cache_entity_config()` / `get_cache_relationships()`.
- Updated `models/utils.py` → `models/dynamodb/utils.py` import path inside `Config._initialize_tables`.
- Added `utils/normalization.py` (`normalize_to_json`) shared by both backend repos.
- Added static adoption guard test (`tests/test_dual_backend_guard.py`) — verifies no direct `models.dynamodb` imports or `insert_update_*` / `delete_*` free-function calls in `queries/`, `mutations/`, `handlers/mcp_handlers.py`.
- Added backend-agnostic dispatch test (DynamoDB arm) — verifies all 4 entities resolve under DynamoDB backend with matching `entity_type`, and `get_loaders` stub returns `None`.
- Compile check passes: `python -m compileall -q mcp_daemon_engine/models` is clean.
- `handlers/mcp_utility.py` was NOT touched (it routes through the GraphQL loopback — backend-agnostic by construction).

Still needed (closeout):

- Add the PostgreSQL arm of the backend-agnostic dispatch test (verify all 4 PG repositories register and resolve with matching `entity_type`).
- Add focused tests proving DynamoDB behavior remains compatible through the dispatch layer (the static adoption guard covers the import boundary; runtime behavior parity through the DynamoDB repository wrappers still needs coverage beyond the existing `test_mcp_daemon_engine.py` suite).

### Phase 2: PostgreSQL Foundation — Complete

Completed:

- Added optional `[postgresql]` extra in `pyproject.toml` (`SQLAlchemy>=1.4`, `psycopg2-binary>=2.9`, `alembic>=1.10`).
- Added `models/postgresql/base.py` (declarative base, `normalize_row`, `_serialize_value`).
- Added PostgreSQL `scoped_session` initialization in `Config` (`_initialize_db_session`) and conditional AWS init (`_initialize_optional_aws_services` — keeping `aws_s3` unconditional when `funct_bucket_name` is set).
- Added Alembic configuration (`migration/alembic.ini`, `migration/alembic/env.py` with `DATABASE_URL > Config > alembic.ini` fallback).
- Added `models/postgresql/utils.py` with PostgreSQL `initialize_tables`.

Still needed:

- Fix the `Config._initialized` reference in `env.py:39` (see "PostgreSQL Schema Principles" note above).
- Validate `Config.initialize(..., db_backend="postgresql")` against a real PostgreSQL service and run `alembic upgrade head`.

### Phase 3: Entity Port — Structurally Complete, Validation Incomplete

Completed:

- Added 4 SQLAlchemy entity models under `models/postgresql/` (`mcp_function.py`, `mcp_module.py`, `mcp_setting.py`, `mcp_function_call.py`).
- Added 4 Alembic migrations (`0001`–`0004`), one per entity, with the indexes listed above. No partial unique indexes (no single-active invariant).
- Added 4 PostgreSQL repository classes under `models/repositories/postgresql/` + `register_all` (importlib-based, matching `rfq_engine`).
- Implemented the S3 content hydration in `MCPFunctionCallPGRepository.get` / `.list` / `.get_type` (read from S3 when `content_in_s3`), and the optional offload in `.insert_update` (only when the caller explicitly sets `content_in_s3=True`).
- Replicated the `purge_cache()` side effect in each PG repository (call `purge_entity_cascading_cache` after a successful commit). `MCPModulePGRepository` also walks `classes` for `setting_id`s and cascades into `mcp_setting`.
- `get_loaders` stub remains — no nested resolvers exist today.

Still needed:

- Add PostgreSQL repository CRUD/list/S3-offload tests against a disposable database (auto-skip without `DATABASE_URL` / `PG_HOST`).
- Add backend-agnostic GraphQL contract tests that run the same suites under both `DB_BACKEND` values.
- **Fix the `*ListType` connection-shape gap:** the PG `list()` methods set only `total` and omit `page_size` / `page_number`; the DynamoDB `resolve_list_decorator` sets all three. Add the two missing fields to each PG `list()` return.
- **Fix the `mcp_function` `desc` filter gap:** the PG `MCPFunctionPGRepository.list` reads `filters.get("description")` but the GraphQL kwarg key is `desc`; the filter is silently broken under PostgreSQL.

### Phase 4: Business Flow Parity — Pending

Required validation under both backends:

- MCP function create/update/delete (tool/resource/prompt types), list by `mcp_type`, list by `desc` (description) / `module_name` / `class_name` / `function_name` filters — **verify the `desc` filter actually fires under PG after the fix**.
- MCP module create/update/delete, list by `package_name`, `module_name` contains filter.
- MCP setting create/update/delete, list by `setting_id` contains filter.
- MCP function call create (with auto-generated UUID), update, delete, list by `mcp_type` / `name` / `status` / `updated_at` window; newest-first ordering.
- S3 content offload round-trip: insert with `content_in_s3=True`, read back with content hydrated from S3.
- `load_mcp_configuration_into_models` end-to-end: a manifest with tools/resources/prompts/modules produces the same row counts and cache-purge behavior under both backends.
- Confirm the MCP runtime (`list_tools` / `list_resources` / `list_prompts` / `process_mcp_message` / `async_execute_tool_function`) behaves identically with `DB_BACKEND=postgresql` (it must — it reads configuration via `Config.fetch_mcp_configuration`, which routes through the internal GraphQL loopback, which routes through the boundary).

### Phase 5: Performance and Operations — Pending

No data migration is in scope (no production DynamoDB data to move). Required:

- Benchmark representative queries/mutations on both backends (function list, function-call list by `updated_at` window, setting get).
- Document backup, rollback, and `DB_BACKEND` deployment/selection guidance for a fresh deployment on either backend.
- Document the S3-offload divergence (PG does not auto-offload on size; existing `content_in_s3=True` rows still hydrate from S3).

### Phase 6: Documentation and Cleanup — Partial

Completed:

- Added `docs/DUAL_BACKEND_CONFIG.md` (backend selection, S3-unconditional-in-PG caveat, S3-offload divergence table).
- Added `docs/POSTGRESQL_SETUP.md` (installation, Alembic migrations, configuration, troubleshooting).
- Added `docs/PHASE0_ENTITY_INVENTORY.md` (per-field type mappings, S3 decision, no `uuid-ossp`, no single-active invariant).
- Updated `README.md` with a dual-backend overview and pointers to the four docs.
- Removed the flat `models/*.py` modules (consolidated under `models/dynamodb/`).

Still needed:

- Add a backend-agnostic contract test reference to the testing documentation.
- Reconcile the `env.py` `Config._initialized` latent bug (see Phase 2 closeout).
- Add the PostgreSQL arm of the dispatch test and PG repository tests (overlaps with Phase 3/4 closeout).

## DataLoaders

`mcp_daemon_engine` has **no nested resolvers** today — the four `types/*.py` files define flat `ObjectType` classes with no resolver methods that fan out to other entities. The dispatch boundary therefore does **not** need a real `get_loaders()` for v1.

The `dispatch.py` module exposes a `get_loaders(context)` stub that returns `None` (`dispatch.py:58-64`) so the seam is forward-compatible. If a nested resolver is later added (e.g. `MCPFunctionType` resolving its parent `MCPModule`), implement:

- `models/dynamodb/batch_loaders/RequestLoaders` + `models/postgresql/batch_loaders/PGRequestLoaders`, each with the new loader property.
- Memoize on `context["batch_loaders"]` (not `context["loaders"]`).
- Migrate the new resolver to import `get_loaders` from `models.repositories.dispatch`.

## Testing Strategy

| Layer | DynamoDB | PostgreSQL |
| --- | --- | --- |
| Import smoke | Dispatch resolves DynamoDB repositories | Dispatch resolves PG repositories |
| Unit | Existing monkey-patched unit tests (`tests/test_mcp_daemon_engine.py`) | Repository normalization and query-building tests |
| Repository | Wrapper parity for existing behavior | SQLAlchemy CRUD/list tests |
| GraphQL | Current schema/query/mutation behavior | Same GraphQL contracts under `DB_BACKEND=postgresql` |
| S3 offload | `content_in_s3` round-trip via DynamoDB + S3 | `content_in_s3` round-trip via PG + S3 (explicit flag) |
| Batch load | `load_mcp_configuration_into_models` parity | Same manifest → same row counts + cache purge |
| Runtime | `list_tools`/`process_mcp_message` unaffected | `list_tools`/`process_mcp_message` unaffected (assert parity) |
| Integration | Reachable DynamoDB | Disposable PostgreSQL database |

Current test coverage:

- `tests/test_dual_backend_guard.py` — static adoption guard (no `models.dynamodb` imports or free-function calls in `queries/`/`mutations/`/`handlers/mcp_handlers.py`) + DynamoDB dispatch smoke (all 4 entities resolve, `get_loaders` stub returns `None`).
- `tests/test_mcp_daemon_engine.py` — existing DynamoDB+S3 integration tests (becomes the DynamoDB arm of the backend-agnostic suite).
- `tests/test_mcp_package_import.py` — existing package-import test.

Minimum next gates:

1. `python -m compileall -q mcp_daemon_engine/models` (currently clean).
2. Import smoke for `get_repo()` under both backends (DynamoDB arm in place; PostgreSQL arm pending).
3. Static adoption guard in `test_dual_backend_guard.py` (in place — fails on re-introduced direct DynamoDB imports).
4. PostgreSQL repository CRUD/list/S3-offload tests against a disposable DB (pending — auto-skip without `DATABASE_URL` / `PG_HOST`).
5. Backend-agnostic GraphQL contract tests under both `DB_BACKEND` settings (pending a live PostgreSQL).
6. Runtime parity test: `list_tools` / `process_mcp_message` return equivalent results regardless of `DB_BACKEND` (pending).

## Acceptance Criteria

Completed or smoke-checked:

- `DB_BACKEND=dynamodb` is the default and dispatch-verified (all 4 repositories resolve with matching `entity_type`).
- `DB_BACKEND=postgresql` has model, repository, migration, and dispatch scaffolding for all 4 entities.
- Repository dispatch registers all 4 DynamoDB repositories (verified by `test_dual_backend_guard.py`).
- GraphQL queries, mutations, and `handlers/mcp_handlers.py` route metadata persistence through `get_repo()` — enforced by the static adoption guard.
- The GraphQL layer and `handlers/mcp_handlers.py` have zero direct `models.dynamodb` imports.
- S3 stays initialized in PG mode when `funct_bucket_name` is set (unconditional, not credential-gated).
- The MCP runtime loopback path (`mcp_utility.py`) is backend-agnostic by construction and was not regressed.
- `BaseModel.Meta` setup moved from `MCPDaemonEngine.__init__` into `Config._initialize_dynamodb_meta`.
- Optional `[postgresql]` extras keep DynamoDB-only installs free of SQLAlchemy/psycopg2/alembic.
- `docs/DUAL_BACKEND_CONFIG.md`, `docs/POSTGRESQL_SETUP.md`, `docs/PHASE0_ENTITY_INVENTORY.md` written; `README.md` updated.

Still required before production readiness:

- PostgreSQL repository, loader, and GraphQL contract tests pass against a real or disposable PostgreSQL database (not yet written).
- The PostgreSQL arm of the backend-agnostic dispatch test is added (only the DynamoDB arm exists today).
- The PG `list()` connection-shape gap is fixed: all 4 PG repos must set `page_size` and `page_number` (not just `total`) to match the DynamoDB `resolve_list_decorator` output.
- The PG `mcp_function` `desc` filter bug is fixed: `MCPFunctionPGRepository.list` must read `filters.get("desc")`, not `filters.get("description")`.
- Cache invalidation behavior is verified for both backends (PG cache config is empty by design; the `purge_entity_cascading_cache` no-op under PG should be confirmed).
- The `env.py` `Config._initialized` latent bug is fixed before relying on the Config-fallback URL resolution path.
- `load_mcp_configuration_into_models` produces identical row counts and cache-purge behavior under both backends (validated).
- The S3 content-offload round-trip is validated under PostgreSQL (explicit `content_in_s3=True` → hydrate from S3).
- The `*ListType` connection shape is asserted identical between DynamoDB and PG `list()` outputs.
- Documentation clearly separates implemented structure from validated runtime parity (this plan does; the remaining docs should cross-reference it).

## Major Risks

| Risk | Severity | Current status | Mitigation |
| --- | --- | --- | --- |
| S3 content offload behavior diverges between backends (DynamoDB auto-offloads on 400KB; PG has no such limit) | Medium | Mitigated — divergence documented and implemented | PG `get`/`list`/`get_type` still hydrates from S3 when `content_in_s3=True`; PG `insert_update` only offloads when the caller explicitly sets the flag. Add a round-trip test. |
| Cascading cache purge (`mcp_module` → `mcp_setting` via `classes.setting_id`) dropped during the port | High | Mitigated — replicated in `MCPModulePGRepository._purge_cache` | Test the cascade under both backends once PG tests are added. |
| `env.py:39` references `Config._initialized` which does not exist on `Config` | Medium | Open — latent bug | Fix by adding an `_initialized` flag to `Config` or replacing the guard; the `try/except` currently swallows it silently, so the Config-fallback URL path never fires. |
| PostgreSQL repository methods drift from DynamoDB decorator behavior (list shape, pagination, ordering) | High | Open — no DB-backed tests yet | Run PG repository tests against a live PostgreSQL; add backend-agnostic GraphQL contract tests. |
| PG `list()` omits `page_size` / `page_number` — connection shape does not match DynamoDB `resolve_list_decorator` output | High | Open — confirmed in source | All 4 PG repos set only `total`. Add `page_size=limit, page_number=page_number` to each PG `list()` return to match the `ListObjectType` contract. |
| PG `MCPFunctionPGRepository.list` reads `filters.get("description")` but the GraphQL kwarg key is `desc` | High | Open — confirmed in source | The `description` filter is silently broken under PostgreSQL. Change the PG repo to read `filters.get("desc")` (matching the DynamoDB model and the `schema.py` arg name). |
| PG `register_all` swallows `ImportError` (carried over from `rfq_engine`), hiding genuine import bugs | Medium | Open | At minimum log the failure; consider failing loudly when `DB_BACKEND=postgresql` is the active backend. |
| Optional PostgreSQL deps leak into DynamoDB-only installs | Medium | Mitigated — `[postgresql]` extra is separate | Keep PG imports lazy; add a DynamoDB-only import test. |
| `mcp_function_call` `updated_at` is a `UTCDateTimeAttribute` but its LSI range is string-typed in DynamoDB | Low | Mitigated — PG uses a real `TIMESTAMP` | Add an ordering parity test (newest-first under both backends). |
| Direct DynamoDB imports reappear in the GraphQL/handler layer | Medium | Mitigated | Static adoption guard in `test_dual_backend_guard.py` fails the build on regression. |

## Immediate Next Work

1. **Fix the PG `list()` connection-shape gap:** add `page_size=limit, page_number=page_number` to all 4 PG `list()` returns so they match the DynamoDB `resolve_list_decorator` output (`ListObjectType` defines `page_size`, `page_number`, `total` — the PG repos currently set only `total`).
2. **Fix the PG `mcp_function` `desc` filter bug:** `MCPFunctionPGRepository.list` reads `filters.get("description")` but the GraphQL schema arg is `desc=String(name="description")`, so the kwarg key is `desc`. Change the PG repo to read `filters.get("desc")` (matching the DynamoDB model at `models/dynamodb/mcp_function.py:172`).
3. Fix the `env.py:39` `Config._initialized` reference (add an `_initialized` class flag to `Config`, set at the end of `initialize()`, or replace the guard).
4. Add the PostgreSQL arm of the backend-agnostic dispatch test (verify all 4 PG repositories register and resolve with matching `entity_type` under `DB_BACKEND=postgresql`).
5. Add PostgreSQL repository CRUD/list/S3-offload tests against a disposable PostgreSQL (`DATABASE_URL` or `PG_HOST`/`PG_*`), starting with `MCPFunctionCallPGRepository` to exercise JSONB (`arguments`), the `updated_at`-window list query, newest-first ordering, and the S3-offload divergence early.
6. Add focused DynamoDB compatibility tests for GraphQL resolvers routing through the repository wrappers (runtime behavior parity, beyond the static guard).
7. Add full backend-agnostic GraphQL contract tests that run the existing DynamoDB suites under `DB_BACKEND=postgresql` as well.
8. Validate `Config.initialize(..., db_backend="postgresql")` against a real PostgreSQL service and run `alembic -c migration/alembic.ini upgrade head`.