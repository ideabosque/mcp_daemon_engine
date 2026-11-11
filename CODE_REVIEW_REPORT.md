# mcp_daemon_engine 综合代码审查报告

> 审查日期：2026-09-08 ｜ 审查范围：项目根目录全部 Python 源码（65 个 .py 文件，约 11,100 行）+ pyproject.toml + Alembic 迁移
> 审查方式：逐文件阅读 + 五轴审查（正确性 / 可读性 / 架构 / 安全性 / 性能）
> 说明：本报告仅审查，未修改任何代码。问题按「严重 / 一般 / 建议 / 信息」分级，并区分 **已确认问题** 与 **待核实项**。

---

## 1. 项目用途、特点与对外使用方式

### 1.1 用途

`mcp_daemon_engine` 是 SilvaEngine 体系中的 **MCP（Model Context Protocol）业务逻辑包**。它本身不承载 HTTP/进程管理（由 `silvaengine_gateway` 负责），而是提供：

1. **MCP 端点的元数据管理**：以 4 个实体（`MCPFunction` 工具/资源/提示定义、`MCPModule` 动态代码模块、`MCPSetting` 模块设置、`MCPFunctionCall` 调用审计）为核心，通过 GraphQL 进行 CRUD。
2. **MCP JSON-RPC 运行时**：对外暴露 `tools/list`、`tools/call`、`resources/*`、`prompts/*` 等 MCP 协议方法，将请求路由到动态加载的 Python 工具类。
3. **动态工具模块装载**：支持 4 种来源 —— `local`（本机 importlib）、`s3`（ZIP 下载解压）、`git`（pip --target 按 commit 隔离安装）、`external`（外部 MCP 服务器的 HTTP 代理适配器）。
4. **SSE 活动推送**：`handlers/sse_manager.py` 提供进程内 SSE 客户端队列管理与跨事件循环投递。

### 1.2 特点

- **双后端持久化**：DynamoDB（PynamoDB，默认）与 PostgreSQL（SQLAlchemy + Alembic + RLS 行级租户隔离）可选，经 `models/repositories/dispatch.py` 的 `get_repo()` 分发边界隔离。
- **分区（partition_key）多租户**：`partition_key = endpoint_id#part_id` 贯穿 GraphQL、JSON-RPC、缓存与 RLS。
- **配置缓存自洽**：`Config.mcp_configuration` 进程内缓存工具/资源/提示/模块清单，写操作（mutation）成功后自动失效。
- **异步工具执行**：`async_execute_tool_function` 支持后台线程或 AWS Lambda Event 两条路径，调用记录落库、可轮询取回。
- **Git 安装工程化较完整**：commit 范围化的 install_target、主机白名单、token 不落日志/库、manifest 校验、TTL 版本检查。

### 1.3 对外使用方式

- **部署元数据**：`main.deploy()` 返回 3 个网关函数定义（`mcp_daemon_graphql`、`mcp`、`async_execute_tool_function`），均 `is_auth_required: False`，认证责任上移至网关层。
- **网关派发入口**（模块级函数）：`dispatch_graphql` / `dispatch_mcp` / `dispatch_sse_message` / `dispatch_mcp_async` / `dispatch_cache_refresh` / `dispatch_cache_clear` / `dispatch_mcp_info`。网关注入 `endpoint_id`、`part_id`、`partition_key`、`context.user`。
- **初始化**：网关启动时调用 `handlers.config.Config.initialize(logger, setting)`；本包无 CLI 入口，通过 `python -m silvaengine_gateway` 运行。
- **GraphQL 面**：9 个 Query（ping、4 组实体查询）+ 16 个 Mutation（4 组 CRUD、loadMcpConfiguration、syncExternalMcpServer、S3 上传两件套、Git 安装三件套）。

---

## 2. 整体架构分层、模块职责与依赖关系

### 2.1 分层结构

```
┌─────────────────────────────────────────────────────────────┐
│ silvaengine_gateway（HTTP/SSE/GraphQL 交付层，外部包）         │
└──────────────┬──────────────────────────────────────────────┘
               │ dispatch_*（模块级入口）+ deploy() 元数据
┌──────────────▼──────────────────────────────────────────────┐
│ 入口层  main.py：MCPDaemonEngine + 7 个 dispatch 函数         │
│   · 参数归一化 _apply_partition_defaults                     │
│   · RLS 上下文设置 / 缓存失效联动                             │
├─────────────────────────────────────────────────────────────┤
│ Schema 层  schema.py（Query/Mutations/type_class）           │
│   ├ queries/    8 个 resolve 函数（部分 @method_cache）       │
│   └ mutations/  16 个 Mutation（薄封装 → handlers）           │
├─────────────────────────────────────────────────────────────┤
│ 业务层  handlers/                                            │
│   · config.py     Config 单例：设置/AWS/DB会话/配置缓存        │
│   · mcp_utility.py 工具执行、参数校验、动态导入、异步执行        │
│   · mcp_git.py    Git 安装/版本检查/刷新                      │
│   · mcp_handlers.py manifest 校验、配置入库、S3 ZIP 处理       │
│   · mcp_external.py 外部 MCP 服务器同步                      │
│   · external_mcp_proxy.py 外部服务器适配器（工具类契约）        │
│   · mcp_server.py  JSON-RPC 方法分发 + stdio 兼容             │
│   · sse_manager.py SSE 客户端管理                            │
├─────────────────────────────────────────────────────────────┤
│ 仓储边界  models/repositories/dispatch.py（get_repo）        │
│   ├ dynamodb/*  PynamoDB 仓储 + cache.py 级联缓存清理          │
│   └ postgresql/* SQLAlchemy 仓储                              │
├─────────────────────────────────────────────────────────────┤
│ 模型层  models/dynamodb/*（PynamoDB 表模型）                  │
│         models/postgresql/*（SQLAlchemy 模型）                │
├─────────────────────────────────────────────────────────────┤
│ 横切  utils/（exceptions、normalization、rls）                │
│       migration/alembic（PG 迁移 0001-0006）                  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 依赖关系要点

- **正向依赖**：main → schema → queries/mutations → repositories → models；handlers.config 被几乎所有模块引用（事实上的全局单例）。
- **反向自环**：`config._dispatch_internal_graphql()` 函数作用域导入 `main.dispatch_graphql`，实现「配置加载走自身 GraphQL schema」的自调用（有意为之，避免循环导入，但形成 main↔config 运行时环）。
- **仓储懒注册**：`dispatch.py` 首次 `get_repo()` 时按后端注册全部 4 个实体仓储，注册不完整时下次重试。
- **外部依赖**：boto3、PynamoDB（经 silvaengine_dynamodb_base）、SQLAlchemy/psycopg2/Alembic（可选 extras）、graphene、mcp SDK v2、mcp-http-client、pendulum、passlib/bcrypt。

---

## 3. 核心工作原理与关键执行流程

### 3.1 配置加载与缓存（读路径的基石）

```
tools/list 等 MCP 方法
  → get_mcp_configuration_with_retry(partition_key)   # 失败清缓存重试 1 次
    → Config.fetch_mcp_configuration(partition_key)   # 进程内缓存命中即返回
      → _dispatch_internal_graphql(MCP_FUNCTION_LIST) # 自调用 GraphQL 查 status=1 函数
      → 按 mcpType 分桶为 tools/resources/prompts
      → _build_module_links + _fetch_modules_and_settings
          # 按模块去重 → 查 MCPModule → 按 class 的 settingId 查 MCPSetting
          # headers 键经 JSONSnakeCase 序列化后恢复原样（保住 HTTP 头大小写）
      → 缓存写入 mcp_configuration[partition_key]（无 TTL，仅显式失效）
```

### 3.2 工具调用（写路径）

```
POST mcp（JSON-RPC tools/call）
  → process_mcp_message → call_tool
    → 校验工具存在于配置 → async? 走异步，否则同步
    → execute_tool_function（@execute_decorator 审计装饰器）
        · partition != "default" 时先写 MCPFunctionCall 审计记录
        · _validate_and_set_defaults 按 inputSchema 填默认值/校验必填
        · _get_class 按 source 分派：local / s3 / git / external 动态导入
        · 实例化工具类，注入 endpoint_id/part_id，调用其方法
        · 按 return_type 包装为 TextContent / ImageContent / EmbeddedResource
        · 成功→审计记录置 completed；失败→置 failed 并附 traceback
```

### 3.3 异步工具执行

`async_execute_tool_function`：已有 uuid 则查记录（completed 直接返回 / 否则返回轮询引用）；否则建审计记录 → Lambda Event 派发或后台线程执行 `execute_tool_function` → 调用方忙轮询 3 秒（0.5s 间隔），超时返回 `mcp://function-call/{uuid}` 资源引用，客户端经 GraphQL `mcpFunctionCall` 继续轮询。

### 3.4 Git 模块安装

`install_mcp_package_from_git`：URL 校验（https/ssh + 主机白名单，拒绝 file://）→ `git ls-remote` 解析 commit（或 latest_tag 策略）→ 与本地持久化元数据比对决定 noop/refresh/installed → `pip install --target {install_path}/{pkg}/{commit} --no-deps` → 补装缺失依赖 → 加载并校验 manifest → Git 元数据注入 setting → 走 `load_mcp_configuration_into_models` 入库 → 预热缓存 → 清理旧 commit 目录。

### 3.5 PostgreSQL 多租户（RLS）

每次 `mcp_daemon_graphql` 调用前置 `SET app.tenant_id = :partition_key`（连接级），finally `db_session.remove()` 归还连接；表策略 `USING (partition_key = current_setting('app.tenant_id', true))`，FORCE RLS 兜底。

---

## 4. 目录结构树

```
mcp_daemon_engine/
├── README.md                        # 运行形态与双后端说明
├── pyproject.toml                   # 打包/依赖/pytest/pyright 配置
├── mcp_daemon_engine_postman_collection.json
├── docs/                            # 9 篇设计与配置文档（未逐字审查）
│   ├── DEVELOPMENT_PLAN.md          DUAL_BACKEND_CONFIG.md
│   ├── DUAL_BACKEND_DEVELOPMENT_PLAN.md  EXTERNAL_MCP_PROXY_DEV_PLAN.md
│   ├── GIT_MODULE_INSTALL_DEV_PLAN.md    MCP_PACKAGE_UPLOAD_SPEC.md
│   ├── PHASE0_ENTITY_INVENTORY.md   POSTGRESQL_SETUP.md
│   └── gateway_restructure_plan.md
├── migration/alembic/               # PG 迁移（0001-0006：4 表 + status 列 + RLS）
└── mcp_daemon_engine/               # 主包（65 个 .py，~11,100 行）
    ├── main.py            (473)  # 入口：MCPDaemonEngine + dispatch_*
    ├── schema.py          (181)  # GraphQL Query/Mutations 装配
    ├── handlers/
    │   ├── config.py     (1102)  # Config 单例：设置/AWS/DB会话/配置缓存
    │   ├── mcp_git.py    (1242)  # Git 安装/版本检查/刷新
    │   ├── mcp_utility.py(1224)  # 工具执行/动态导入/异步执行/参数校验
    │   ├── mcp_handlers.py(655)  # manifest 校验/配置入库/S3 ZIP 处理
    │   ├── mcp_server.py  (517)  # JSON-RPC 分发 + stdio 适配
    │   ├── sse_manager.py (293)  # SSE 客户端管理与跨 loop 投递
    │   ├── mcp_external.py(232)  # 外部 MCP 服务器同步
    │   └── external_mcp_proxy.py(195) # 外部服务器工具类适配器
    ├── queries/          # 4 组 resolve（部分 @method_cache TTL 1800s）
    ├── mutations/        # 8 个 Mutation 模块（薄封装）
    ├── types/            # 5 个 graphene 类型模块
    ├── models/
    │   ├── dynamodb/     # PynamoDB 表模型 + cache.py 级联缓存清理
    │   ├── postgresql/   # SQLAlchemy 模型 + base/utils
    │   └── repositories/ # dispatch.py + base.py + dynamodb/* + postgresql/*
    └── utils/            # exceptions / normalization / rls
```

### 关键文件清单（按重要性）

| 文件 | 行数 | 职责 | 审查深度 |
|---|---|---|---|
| `handlers/mcp_utility.py` | 1224 | 工具执行核心、动态导入、异步执行 | 全文 |
| `handlers/mcp_git.py` | 1242 | Git 安装全流程 | 全文 |
| `handlers/config.py` | 1102 | 全局配置/缓存/AWS/DB 会话 | 全文 |
| `handlers/mcp_handlers.py` | 655 | manifest 校验、配置入库、S3 上传 | 全文 |
| `handlers/mcp_server.py` | 517 | JSON-RPC 方法分发 | 全文 |
| `main.py` | 473 | 入口与派发 | 全文 |
| `handlers/sse_manager.py` | 293 | SSE 管理 | 全文 |
| `handlers/mcp_external.py` / `external_mcp_proxy.py` | 427 | 外部服务器同步与代理 | 全文 |
| `models/repositories/*`（dispatch/base + PG 4 仓储） | ~1000 | 双后端仓储 | 核心 5 文件全文，DynamoDB 仓储抽样 |
| `models/dynamodb/mcp_function_call.py` | 426 | DynamoDB 模型与索引 | 关键段 |
| `utils/rls.py`、`models/postgresql/utils.py` | 131 | RLS 与建表 | 全文 |
| 迁移脚本 0001-0006、env.py | ~350 | PG 迁移 | 0006 全文，其余抽样 |
| queries/、mutations/、types/ | ~900 | GraphQL 面 | 全部抽样核对装配关系 |

---

## 5. 缺陷、不足与改进建议

### 5.1 已确认问题

#### 严重

**[严重] #1 配置缓存加载对空分区必然崩溃**
- 位置：`handlers/config.py:776-788`（`fetch_mcp_configuration`）
- 问题：当 GraphQL 返回 `data` 但 `mcpFunctionList.mcpFunctionList` 为 `None`（分区下无任何已启用函数）时，仅打 warning 不返回，随后 `for func in mcp_functions:` 对 `None` 迭代抛 `TypeError`。
- 影响：空分区无法完成初始化，`process_mcp_message` 直接返回 Internal error，`mcp_info`/`tools/list` 等全部不可用；且失败结果不缓存，每次请求都重复失败。
- 建议：`mcp_functions` 非 list 时置为 `[]` 并提前构建空配置写入缓存后返回。

**[严重] #2 弱默认安全凭据**
- 位置：`handlers/config.py:320-326`
- 问题：`jwt_secret_key` 默认 `"CHANGEME"`、`admin_username` 默认 `"admin"`、`admin_password` 默认 `"admin123"`。
- 影响：一旦这些配置被认证链路（网关或未来本包内认证）消费且运维未显式覆盖，等于公开凭据。即使当前认证逻辑不在本包，默认值本身构成"配置陷阱"。
- 建议：默认值改为 `None` 并在缺失时 fail-fast 或至少 ERROR 级日志告警。

**[严重] #3 测试完全缺失**
- 位置：`pyproject.toml` `[tool.pytest.ini_options] testpaths = ["mcp_daemon_engine/tests"]`，但该目录不存在。
- 影响：约 11,100 行业务代码零测试；`pytest` 在当前配置下收集不到任何用例。审查中发现的多个问题（#1、#4、#6）均属单测可拦截类别。
- 建议：优先为 `fetch_mcp_configuration`、`_validate_and_set_defaults`、`_validate_git_url`、`validate_manifest`、SSE 管理器、双后端仓储 `insert_update/delete` 建立最小测试面。

#### 一般

**[一般] #4 死代码与无效导入/依赖**
- `handlers/config.py:5`：`from operator import delitem` 从未使用（且语义突兀）。
- `handlers/config.py:4 与 767`：`traceback` 模块级导入后又在函数内重复导入。
- `handlers/mcp_utility.py:559-563`：`raise ImportError(...)` 之后紧跟第二个完全相同的 `raise`，不可达死代码。
- `utils/exceptions.py`：`AuthenticationError`/`TokenExpiredError`/`RateLimitExceeded` 定义并导出，但包内无任何抛出点。
- `pyproject.toml`：`python-jose[cryptography]` 声明依赖在包内无任何 import（认证疑由网关承担）。
- 建议：删除无效导入与不可达分支；异常类若为网关契约保留则在注释中说明，否则删除；依赖项移除或移入说明文档。

**[一般] #5 进程内配置缓存无 TTL 且多进程失效不传播**
- 位置：`handlers/config.py:207,745`（`mcp_configuration` 字典）
- 问题：缓存仅在"本进程内 mutation 成功"时失效。多 worker（gunicorn/多 Lambda 实例）部署下，A 进程的写操作无法清掉 B 进程的缓存，B 进程将无限期返回旧工具清单（`CACHE_TTL` 只作用于 query 的 `method_cache`，不作用于该缓存）。
- 建议：为配置缓存加 TTL 兜底（如复用 `CACHE_TTL`），或引入共享失效信号（版本号比对 / pub-sub）。

**[一般] #6 RLS 使用连接级 SET，连接池复用存在租户上下文残留**
- 位置：`utils/rls.py:30-42`、`handlers/config.py:552-557`
- 问题：`SET app.tenant_id`（非 `SET LOCAL`）随连接归还连接池后依然生效。请求 A 设置租户后，若请求 B 未携带 `partition_key`（不触发 `_set_rls_context`）而复用该连接，B 的查询将落在 A 的租户数据上。
- 缓解因素：主链路均经 `mcp_daemon_graphql` 统一设置 RLS，且 `current_setting(..., true)` 未设置时策略匹配 NULL 行（安全方向）；但 `partition_key` 缺失的旁路（如直接调用仓储层）无防护。
- 建议：所有仓储入口强制要求 partition_key（缺失即抛错）；或改用每事务 `SET LOCAL` + 显式事务包裹。

**[一般] #7 `latest_tag` 版本策略按字典序排序，可能选错"最新"标签**
- 位置：`handlers/mcp_git.py:362`（`tags.sort(key=lambda t: t[0], reverse=True)`）
- 问题：`v2.9` > `v2.10`（字典序），语义化版本号场景会安装旧版。
- 建议：解析为版本元组排序（`re.split` 数字段 / `packaging.version`）。

**[一般] #8 `execute_decorator` 异常路径可能掩盖原始异常**
- 位置：`handlers/mcp_utility.py:381-397`
- 问题：except 分支内再次调用 `_insert_update_mcp_function_call` 写失败状态，若该写库调用自身抛错，将覆盖原始业务异常；且传入的 `updatedBy` kwarg 在更新分支中并未被使用（函数内部硬编码 `"mcp_daemon_engine"`）。
- 建议：写审计失败用 try/except 包裹仅记日志；去掉无效 kwarg。

**[一般] #9 异步执行轮询实现粗糙且注释失实**
- 位置：`handlers/mcp_utility.py:1173-1203`
- 问题：① 注释写"60 second timeout"，实际 `while ... <= 3` 为 3 秒；② 忙轮询每 0.5s 全量查一次审计记录；③ `initial→in_process` 的状态推进写在轮询循环内，每轮都可能触发一次更新调用。
- 建议：修正注释；状态推进移出循环只做一次；轮询间隔可指数退避。

**[一般] #10 设置聚合存在键冲突后写覆盖**
- 位置：`handlers/mcp_handlers.py:180-204`
- 问题：`load_mcp_configuration_into_models` 将 manifest 中所有 module 的 `setting` 合并为**一条共享设置记录**，同名键后写覆盖。Git manifest 多模块或多个外部服务器共用 `base_url` 等键时相互污染（后续模块运行时会拿到前一个模块的连接配置）。
- 建议：按 module 维度各建设置记录（module.classes 已支持按 class 挂 setting_id），或在合并时对冲突键告警。

**[一般] #11 外部服务器凭据明文入库且可经 GraphQL 读回**
- 位置：`handlers/mcp_external.py:210-222`（`bearer_token` 写入 variables → setting）；`queries/mcp_setting` 可原样返回
- 影响：能访问 `mcpSetting` 查询的主体可读取上游服务器凭据；数据库泄露即凭据泄露。
- 建议：凭据脱敏返回（读路径掩码），或拆分 Secret 存储/引用。

**[一般] #12 动态导入的全局 `sys.path` 污染**
- 位置：`handlers/mcp_utility.py:566-567, 610-611`（`sys.path.insert(0, ...)` 后不清理）
- 问题：与 `_import_mcp_configuration_from_dir`（有完整的 save/restore）形成对比。install_target 长期留在 `sys.path[0]`，且模块名可遮蔽标准库/包内同名模块，跨模块名冲突排查困难。
- 建议：参照 `_import_mcp_configuration_from_dir` 的模式恢复 sys.path；导入用 `spec_from_file_location` 已可避免多数 path 注入。

**[一般] #13 内部错误细节外泄**
- 位置：`handlers/mcp_server.py:499-503`
- 问题：`process_mcp_message` 异常响应的 `data` 字段直接放 `str(e)`，可能包含文件路径、SQL 片段等内部信息。
- 建议：对外返回通用 message + 关联 ID，详情仅入日志。

#### 建议（值得考虑）

**[建议] #14 超大文件拆分**：`mcp_git.py`(1242)、`mcp_utility.py`(1224)、`config.py`(1102) 均超 1000 行信号线。`config.py` 中 `_normalize_schema_keywords`/`_to_snake_case`（~140 行纯函数）与 Git 认证可拆为独立模块。

**[建议] #15 camel→snake 转换重复实现且行为不一致**：`config._to_snake_case`（`(?<!^)(?=[A-Z])`）与 `mcp_utility._camel_to_snake`（处理连续大写）并存，对 `HTTPURL` 类输入结果不同。统一保留后者并复用。

**[建议] #16 Config 全局可变单例损害可测试性**：类属性即全局状态（缓存、backend、logger、session）。至少为 `fetch_mcp_configuration` 增加注入点，便于测试隔离。

**[建议] #17 内部 GraphQL 自调用开销**：每次配置加载经过完整 graphene 解析/校验/序列化（含多次 `_dispatch_internal_graphql` 往返：1 次函数列表 + N 模块 + N 设置）。可直接调用 repo 层获得相同数据，减少延迟与故障面。

**[建议] #18 `_active_threads` 无锁并发访问**：`mcp_utility.py:37,1165-1171` 多线程 append/切片清理无同步。竞态影响小（仅监控用途），但可改用 `concurrent.futures` 或加锁。

**[建议] #19 `except Exception: pass` 静默吞掉缓存清理失败**：PG 仓储 `_purge_cache`（如 `mcp_function_repo.py:269-270`）。至少 debug 级日志。

**[建议] #20 每次调用新建 ThreadPoolExecutor**：`external_mcp_proxy.py:134`、`mcp_utility.py:819`。高频路径建议复用池或改用 `asyncio.run_coroutine_threadsafe`。

**[建议] #21 pip 令牌经命令行参数暴露**：`mcp_git.py:398-408` 中含 token 的 pip direct URL 出现在进程参数列表（`ps` 可见）。可改用 `GIT_TOKEN` 环境变量 + git credential helper 或 netrc。

#### 信息

- **[信息]** `handlers/mcp_server.py` 中对 SDK v1/v2 兼容的 fallback 设计（`_make_server` 降级为 bare Server）与 `_serialize_sdk_model` 的 alias 策略是合理的 Chesterton's Fence，勿轻易"简化"。
- **[信息]** `sse_manager` 用 `threading.Lock` + `call_soon_threadsafe` 跨事件循环投递的设计正确且注释充分；唯一假设是"单一消费者 loop"（`self._loop` 只记录第一个），多 loop 消费场景需重新审视。
- **[信息]** `handlers/mcp_utility.py:1079` 存在 TODO："Rebuild the function to support async execution with proper thread management and cleanup"，与 #9 呼应。

### 5.2 待核实项（需运行环境/网关行为确认）

| # | 事项 | 核实方法 |
|---|---|---|
| Q1 | **Lambda 异步路径疑似死锁**：`mcp_utility.py:1128-1149` Lambda 分支不执行本地线程；被 Event 派发的 Lambda 再次进入 `async_execute_tool_function` 时记录状态为 initial → `_async_exec` 直接返回"未完成"引用而不执行工具，工具可能永远不执行。 | 部署 `aws_lambda` + is_async 工具实测一次完整调用链，确认 Lambda 侧是否有独立执行入口 |
| Q2 | `deploy()` 三个端点 `is_auth_required: False`，本包无任何认证逻辑（见 #4）。需确认网关在 `settings: "beta_core_ai_agent"` 下是否强制认证，否则 `dispatch_mcp` 为无鉴权的远程代码执行面（工具类由配置驱动动态加载）。 | 审查 silvaengine_gateway 路由/中间件 |
| Q3 | RLS 连接残留（#6）的实际影响取决于网关 worker 模型与是否存在 partition_key 缺失的调用路径。 | 在 PG 模式下用两个分区交叉请求验证 |
| Q4 | 缓存失效不传播（#5）是否实际发生取决于网关进程数；单进程部署则暂无影响。 | 确认部署拓扑 |
| Q5 | PG 模式下若未配置 `funct_bucket_name` 但存在 `content_in_s3=true` 的历史行，`_fetch_content_from_s3` 将对 `None` 调 `get_object` 报 AttributeError。 | 确认 PG 部署是否始终配置 S3 |
| Q6 | `Config.fetch_mcp_configuration` 的 `mcp_configuration["default"]` 键（config.py:335）与分区键空间可能冲突（某分区恰好叫 "default" 时两套缓存互相覆盖）。 | 确认网关是否可能产生字面 "default" 分区 |

### 5.3 优先级改进路线（最小改动原则）

1. **P0（可立即落地、改动极小）**：#1 空分区判空返回；#2 弱默认凭据改 fail-fast；#13 错误信息脱敏；#4 死代码清理。
2. **P1**：#3 建立最小测试面（先覆盖 config 加载与 manifest 校验）；#7 tag 版本排序；#8/#9 装饰器与轮询修复。
3. **P2（需设计讨论）**：#5 缓存 TTL/失效传播；#10 设置按模块拆分；#6 RLS 语义收紧；#11 凭据脱敏。

---

## 6. 总体评价

架构分层清晰：入口/Schema/业务/仓储边界/模型五层职责明确，双后端经 `get_repo()` 边界隔离良好，repository 归一化 dict 契约、懒注册失败重试、SSE 跨事件循环投递、Git 安装的 commit 隔离与主机白名单等设计均属最佳实践水准；文档（docs/ 9 篇）与代码高度同步。主要短板集中在三处：**错误/边界路径处理不足**（空分区崩溃、异常掩盖、错误外泄）、**进程内缓存的分布式语义缺失**、**测试为零**。按 5.3 的 P0→P2 路线推进可在不引入过度设计的前提下显著收敛风险。