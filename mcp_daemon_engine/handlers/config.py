# -*- coding: utf-8 -*-
from __future__ import print_function

import traceback
from operator import delitem

__author__ = "bibow"

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import boto3
from passlib.context import CryptContext
from pydantic import AnyUrl

from silvaengine_utility import Debugger, JSONSnakeCase, Serializer

MCP_FUNCTION_LIST = """query mcpFunctionList(
        $pageNumber: Int,
        $limit: Int,
        $mcpType: String,
        $moduleName: String,
        $className: String,
        $functionName: String
    ) {
    mcpFunctionList(
        pageNumber: $pageNumber,
        limit: $limit,
        mcpType: $mcpType,
        moduleName: $moduleName,
        className: $className,
        functionName: $functionName
    ) {
        pageSize
        pageNumber
        total
        mcpFunctionList {
            partitionKey
            name
            mcpType
            description
            data
            annotations
            moduleName
            className
            functionName
            returnType
            isAsync
            updatedBy
            createdAt
            updatedAt
        }
    }
}"""

MCP_MODULE = """query mcpModule($moduleName: String!) {
    mcpModule(moduleName: $moduleName) {
        partitionKey
        moduleName
        packageName
        classes
        source
        updatedBy
        createdAt
        updatedAt
    }
}"""

MCP_SETTING = """query mcpSetting($settingId: String!) {
    mcpSetting(settingId: $settingId) {
        partitionKey
        settingId
        setting
        updatedBy
        createdAt
        updatedAt
    }
}"""

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


@dataclass
class LocalUser:
    username: str
    password_hash: str
    roles: List[str]

    def verify(self, plain: str) -> bool:
        return _pwd.verify(plain, self.password_hash)


class Config:
    """
    Centralized Configuration Class
    Manages shared configuration variables across the application.
    """

    # Backend selection: "dynamodb" (default) or "postgresql"
    DB_BACKEND: str = "dynamodb"

    # PostgreSQL session (only initialized when DB_BACKEND == "postgresql")
    db_session = None

    # Cache Configuration
    CACHE_TTL = 1800
    CACHE_ENABLED = True

    CACHE_NAMES = {
        "models": "mcp_daemon_engine.models.dynamodb",
        "queries": "mcp_daemon_engine.queries",
    }

    # ------------------------------------------------------------------
    # Cache entity metadata — backend-aware.
    #
    # The PostgreSQL repositories do not currently use @method_cache,
    # so CACHE_ENTITY_CONFIG_POSTGRESQL is intentionally empty.
    # ------------------------------------------------------------------
    CACHE_ENTITY_CONFIG_DYNAMODB = {
        "mcp_function": {
            "module": "mcp_daemon_engine.models.dynamodb.mcp_function",
            "model_class": "MCPFunctionModel",
            "getter": "get_mcp_function",
            "list_resolver": "mcp_daemon_engine.queries.mcp_function.resolve_mcp_function_list",
            "cache_keys": ["context:partition_key", "key:name"],
        },
        "mcp_module": {
            "module": "mcp_daemon_engine.models.dynamodb.mcp_module",
            "model_class": "MCPModuleModel",
            "getter": "get_mcp_module",
            "list_resolver": "mcp_daemon_engine.queries.mcp_module.resolve_mcp_module_list",
            "cache_keys": ["context:partition_key", "key:module_name"],
        },
        "mcp_function_call": {
            "module": "mcp_daemon_engine.models.dynamodb.mcp_function_call",
            "model_class": "MCPFunctionCallModel",
            "getter": "get_mcp_function_call",
            "list_resolver": "mcp_daemon_engine.queries.mcp_function_call.resolve_mcp_function_call_list",
            "cache_keys": ["context:partition_key", "key:mcp_function_call_uuid"],
        },
        "mcp_setting": {
            "module": "mcp_daemon_engine.models.dynamodb.mcp_setting",
            "model_class": "MCPSettingModel",
            "getter": "get_mcp_setting",
            "list_resolver": "mcp_daemon_engine.queries.mcp_setting.resolve_mcp_setting_list",
            "cache_keys": ["context:partition_key", "key:setting_id"],
        },
    }

    # PostgreSQL cache config — empty until PG repos opt into caching.
    CACHE_ENTITY_CONFIG_POSTGRESQL: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def get_cache_entity_config(cls) -> Dict[str, Dict[str, Any]]:
        """Return cache metadata for the active DB_BACKEND."""
        if cls.DB_BACKEND == "postgresql":
            return cls.CACHE_ENTITY_CONFIG_POSTGRESQL
        return cls.CACHE_ENTITY_CONFIG_DYNAMODB

    # ------------------------------------------------------------------
    # Entity cache dependency relationships — backend-aware.
    # ------------------------------------------------------------------
    CACHE_RELATIONSHIPS_DYNAMODB = {
        "mcp_module": [
            {
                "entity_type": "mcp_function",
                "module": "mcp_function",
                "list_resolver": "resolve_mcp_function_list",
                "dependency_key": "module_name",
                "parent_key": "module_name",
            }
        ],
        "mcp_function": [
            {
                "entity_type": "mcp_function_call",
                "module": "mcp_function_call",
                "list_resolver": "resolve_mcp_function_call_list",
                "dependency_key": "name",
                "parent_key": "name",
            }
        ],
    }

    # PostgreSQL cascade relationships — empty until PG repos cache list resolvers.
    CACHE_RELATIONSHIPS_POSTGRESQL: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def get_cache_relationships(cls) -> Dict[str, List[Dict[str, Any]]]:
        """Return cascade-invalidation relationships for the active backend."""
        if cls.DB_BACKEND == "postgresql":
            return cls.CACHE_RELATIONSHIPS_POSTGRESQL
        return cls.CACHE_RELATIONSHIPS_DYNAMODB

    setting: Dict[str, Any] = {}

    transport = None
    port = None
    mcp_configuration = {}
    funct_bucket_name = None
    funct_zip_path = None
    funct_extract_path = None
    logger = None
    aws_s3 = None
    aws_cognito_idp = None
    aws_lambda = None

    # ----------------- universal -----------------
    auth_provider: str | None = None  # "local" | "cognito" | "api_gateway"

    # -------- local-JWT (HS256) settings ---------
    jwt_secret_key: str | None = None
    jwt_algorithm: str | None = None
    access_token_exp: int | None = None  # minutes

    # local users file
    local_user_file: str | None = None
    _USERS = None

    # static super-admin
    admin_username: str | None = None
    admin_password: str | None = None
    admin_static_token: str | None = None

    # ------------- Cognito settings --------------
    issuer = None
    cognito_app_client_id: str | None = None
    cognito_app_secret: str | None = None
    jwks_endpoint: AnyUrl | None = None
    jwks_cache_ttl: int | None = None  # seconds

    @classmethod
    def initialize(cls, logger: logging.Logger, setting: Dict[str, Any]) -> None:
        """
        Initialize configuration setting.

        Backend selection is driven by ``setting["db_backend"]``:
        - ``dynamodb`` (default): preserves current PynamoDB behavior.
        - ``postgresql``: uses SQLAlchemy scoped session for persistence.

        Args:
            logger (logging.Logger): Logger instance for logging.
            setting (Dict[str, Any]): Configuration dictionary.
        """
        try:
            cls.logger = logger
            cls.setting = setting
            cls._set_parameters(setting)
            cls._setup_function_paths(setting)

            # Read backend selection (deployment-time, not per request)
            cls.DB_BACKEND = str(setting.get("db_backend", "dynamodb")).lower()

            if cls.DB_BACKEND == "dynamodb":
                cls._initialize_aws_services(logger, setting)
                cls._initialize_dynamodb_meta(setting)
            elif cls.DB_BACKEND == "postgresql":
                cls._initialize_optional_aws_services(setting)
                cls._initialize_db_session(setting)
            else:
                raise ValueError(f"Unknown db_backend: {cls.DB_BACKEND}")

            if cls.transport == "sse" and cls.auth_provider == "local":
                cls._USERS = cls._load()

            if setting.get("initialize_tables"):
                cls._initialize_tables(logger)

            logger.info(
                f"Configuration initialized successfully (db_backend={cls.DB_BACKEND})."
            )
        except Exception as e:
            logger.exception("Failed to initialize configuration.")
            raise e

    @classmethod
    def _set_parameters(cls, setting: Dict[str, Any]) -> None:
        """
        Set application-level parameters.
        Args:
            setting (Dict[str, Any]): Configuration dictionary.
        """

        cls.transport = setting.get("transport", "sse")
        cls.port = setting.get("port", 8000)
        cls.auth_provider = setting.get("auth_provider", "local")  # "local" | "cognito"
        cls.jwt_secret_key = setting.get("jwt_secret_key", "CHANGEME")
        cls.jwt_algorithm = setting.get("jwt_algorithm", "HS256")
        cls.access_token_exp = int(setting.get("access_token_exp", 15))
        cls.local_user_file = setting.get("local_user_file", "users.json")
        cls.admin_username = setting.get("admin_username", "admin")
        cls.admin_password = setting.get("admin_password", "admin123")
        cls.admin_static_token = setting.get("admin_static_token", None)
        cls.cognito_app_client_id = setting.get("cognito_app_client_id", None)
        cls.cognito_app_secret = setting.get("cognito_app_secret", None)
        cls.jwks_cache_ttl = int(setting.get("jwks_cache_ttl", 3600))

        if "cache_enabled" in setting:
            cls.CACHE_ENABLED = setting.get("cache_enabled", True)

        if setting.get("mcp_configuration") is not None:
            cls.mcp_configuration["default"] = setting["mcp_configuration"]
            cls.logger.info("MCP Configuration loaded successfully.")

    @classmethod
    def _setup_function_paths(cls, setting: Dict[str, Any]) -> None:
        cls.funct_bucket_name = setting.get("funct_bucket_name")
        cls.logger.info(
            f"_setup_function_paths: funct_bucket_name={cls.funct_bucket_name!r}, "
            f"setting keys present={sorted(setting.keys())[:10]}..., "
            f"'funct_bucket_name' in setting={'funct_bucket_name' in setting}"
        )
        cls.funct_zip_path = (
            "/tmp/funct_zips"
            if setting.get("funct_zip_path") is None
            or setting.get("funct_zip_path") == ""
            else setting["funct_zip_path"]
        )
        cls.funct_extract_path = (
            "/tmp/functs"
            if setting.get("funct_extract_path") is None
            or setting.get("funct_extract_path") == ""
            else setting["funct_extract_path"]
        )
        os.makedirs(cls.funct_zip_path, exist_ok=True)
        os.makedirs(cls.funct_extract_path, exist_ok=True)

    @classmethod
    def _initialize_aws_services(
        cls, logger: logging.Logger, setting: Dict[str, Any]
    ) -> None:
        """
        Initialize AWS services including S3 and Cognito IDP clients.
        Args:
            logger (logging.Logger): Logger instance for logging
            setting (Dict[str, Any]): Configuration dictionary containing AWS credentials and settings
        """
        try:
            if all(
                setting.get(k)
                for k in ["region_name", "aws_access_key_id", "aws_secret_access_key"]
            ):
                aws_credentials = {
                    "region_name": setting["region_name"],
                    "aws_access_key_id": setting["aws_access_key_id"],
                    "aws_secret_access_key": setting["aws_secret_access_key"],
                }
            else:
                aws_credentials = {}

            cls.aws_s3 = boto3.client(
                "s3",
                **aws_credentials,
                config=boto3.session.Config(signature_version="s3v4"),
            )

            if (
                all(setting.get(k) for k in ["region_name", "cognito_user_pool_id"])
                and cls.auth_provider == "cognito"
            ):
                cls.issuer = f"https://cognito-idp.{setting['region_name']}.amazonaws.com/{setting['cognito_user_pool_id']}"
                cls.jwks_endpoint = (
                    setting.get("cognito_jwks_url")
                    or f"{cls.issuer}/.well-known/jwks.json"
                )
                cls.aws_cognito_idp = boto3.client(
                    "cognito-idp", region_name=setting["region_name"]
                )

            if cls.auth_provider == "api_gateway":
                cls.aws_lambda = boto3.client("lambda", **aws_credentials)
        except Exception as e:
            logger.exception("Failed to initialize AWS services configuration.")
            raise e

    @classmethod
    def _initialize_dynamodb_meta(cls, setting: Dict[str, Any]) -> None:
        """Initialize PynamoDB BaseModel.Meta credentials from setting."""
        from silvaengine_dynamodb_base import BaseModel

        if (
            setting.get("region_name")
            and setting.get("aws_access_key_id")
            and setting.get("aws_secret_access_key")
        ):
            BaseModel.Meta.region = setting.get("region_name")
            BaseModel.Meta.aws_access_key_id = setting.get("aws_access_key_id")
            BaseModel.Meta.aws_secret_access_key = setting.get(
                "aws_secret_access_key"
            )

    @classmethod
    def _initialize_optional_aws_services(
        cls, setting: Dict[str, Any]
    ) -> None:
        """Initialize AWS services in PG mode.

        S3 stays initialized unconditionally when funct_bucket_name is set
        (mcp_daemon_engine needs S3 for package uploads + content offload even
        in PG mode). Other AWS clients (Cognito, Lambda) remain conditional on
        credentials + auth provider.
        """
        try:
            creds_present = all(
                setting.get(k)
                for k in [
                    "region_name",
                    "aws_access_key_id",
                    "aws_secret_access_key",
                ]
            )
            if creds_present:
                aws_credentials = {
                    "region_name": setting["region_name"],
                    "aws_access_key_id": setting["aws_access_key_id"],
                    "aws_secret_access_key": setting["aws_secret_access_key"],
                }
            else:
                aws_credentials = {}

            # S3 — unconditional when funct_bucket_name is set
            if cls.funct_bucket_name:
                cls.aws_s3 = boto3.client(
                    "s3",
                    **aws_credentials,
                    config=boto3.session.Config(signature_version="s3v4"),
                )

            # Cognito — conditional on creds + auth_provider
            if (
                creds_present
                and all(
                    setting.get(k) for k in ["region_name", "cognito_user_pool_id"]
                )
                and cls.auth_provider == "cognito"
            ):
                cls.issuer = f"https://cognito-idp.{setting['region_name']}.amazonaws.com/{setting['cognito_user_pool_id']}"
                cls.jwks_endpoint = (
                    setting.get("cognito_jwks_url")
                    or f"{cls.issuer}/.well-known/jwks.json"
                )
                cls.aws_cognito_idp = boto3.client(
                    "cognito-idp", region_name=setting["region_name"]
                )

            # Lambda — conditional on creds + api_gateway auth
            if creds_present and cls.auth_provider == "api_gateway":
                cls.aws_lambda = boto3.client("lambda", **aws_credentials)

        except Exception as e:
            cls.logger.exception("Failed to initialize optional AWS services.")
            raise e

    @classmethod
    def _initialize_db_session(cls, setting: Dict[str, Any]) -> None:
        """Initialize the PostgreSQL database session using SQLAlchemy.

        Expected setting keys: db_host, db_port, db_user, db_password, db_schema.
        """
        from urllib.parse import quote_plus

        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker

        password = quote_plus(setting["db_password"])
        connection_string = (
            f"postgresql+psycopg2://{setting['db_user']}:{password}"
            f"@{setting['db_host']}:{setting['db_port']}/{setting['db_schema']}"
        )

        engine = create_engine(
            connection_string,
            pool_recycle=7200,
            pool_size=10,
            pool_pre_ping=True,
            echo=False,
        )

        cls.db_session = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )

    @classmethod
    def _initialize_tables(cls, logger: logging.Logger) -> None:
        """Initialize database tables by calling the backend-appropriate method."""
        if cls.DB_BACKEND == "dynamodb":
            from ..models.dynamodb.utils import initialize_tables

            initialize_tables(logger)
        elif cls.DB_BACKEND == "postgresql":
            from ..models.postgresql.utils import initialize_tables as pg_init

            pg_init(logger, cls.db_session)

    @classmethod
    def _load(cls) -> dict[str, LocalUser]:
        p = Path(cls.local_user_file).expanduser()

        if not p.exists():
            cls.logger.warning(
                f"Local user file not found: {p} — no local users loaded. "
                f"Admin credentials will still work via environment config."
            )
            return {}

        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        return {
            u["username"]: LocalUser(**u)
            for u in raw
            if isinstance(u, dict) and "username" in u
        }

    @classmethod
    def _to_snake_case(cls, camel_str: str) -> str:
        """Convert camelCase string to snake_case."""
        import re

        # Insert underscore before uppercase letters and convert to lowercase
        snake_str = re.sub(r"(?<!^)(?=[A-Z])", "_", camel_str).lower()
        return snake_str

    @classmethod
    def _normalize_schema_keywords(cls, schema: Any) -> Any:
        """
        Recursively convert keyword values to proper data types in JSON Schema.
        Handles nested arrays and objects.
        Converts property keys from camelCase to snake_case.
        """
        if not isinstance(schema, dict):
            return schema

        normalized = {}

        # Keywords that should be integers
        integer_keywords = {
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
            "minContains",
            "maxContains",
        }

        # Keywords that should be numbers (int or float)
        number_keywords = {
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
        }

        # Keywords that should be booleans
        boolean_keywords = {"uniqueItems", "additionalProperties"}

        # Keywords that should be decimals/numbers
        decimal_keywords = {"default", "const"}

        for key, value in schema.items():
            # Convert to proper data type based on keyword
            if key in integer_keywords:
                if isinstance(value, int):
                    normalized[key] = value
                else:
                    try:
                        normalized[key] = int(value)
                    except (ValueError, TypeError):
                        normalized[key] = value
            elif key in number_keywords:
                if isinstance(value, (int, float)):
                    normalized[key] = value
                else:
                    try:
                        # Try int first, then float
                        normalized[key] = (
                            int(value)
                            if isinstance(value, str) and value.isdigit()
                            else float(value)
                        )
                    except (ValueError, TypeError):
                        normalized[key] = value
            elif key in decimal_keywords:
                # Handle default and const values - convert if they are numeric strings
                if isinstance(value, (int, float)):
                    normalized[key] = value
                elif isinstance(value, str):
                    try:
                        # Try to convert to number if it's a numeric string
                        normalized[key] = (
                            int(value) if value.isdigit() else float(value)
                        )
                    except (ValueError, TypeError):
                        # Keep as string if conversion fails
                        normalized[key] = value
                else:
                    # Keep the value as-is for other types (bool, list, dict, etc.)
                    normalized[key] = value
            elif key in boolean_keywords:
                if isinstance(value, str):
                    normalized[key] = value.lower() in ("true", "1", "yes")
                elif isinstance(value, bool):
                    normalized[key] = value
                else:
                    try:
                        normalized[key] = bool(value)
                    except (ValueError, TypeError):
                        normalized[key] = value
            # Recursive handling for nested structures
            elif key == "properties" and isinstance(value, dict):
                # Recursively normalize each property schema and convert keys to snake_case
                normalized[key] = {
                    cls._to_snake_case(prop_name): cls._normalize_schema_keywords(
                        prop_schema
                    )
                    for prop_name, prop_schema in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                # Recursively normalize array item schema
                normalized[key] = cls._normalize_schema_keywords(value)
            elif key == "additionalItems" and isinstance(value, dict):
                # Recursively normalize additional items schema
                normalized[key] = cls._normalize_schema_keywords(value)
            elif key == "patternProperties" and isinstance(value, dict):
                # Recursively normalize pattern properties
                normalized[key] = {
                    pattern: cls._normalize_schema_keywords(prop_schema)
                    for pattern, prop_schema in value.items()
                }
            elif key == "contains" and isinstance(value, dict):
                # Recursively normalize contains schema
                normalized[key] = cls._normalize_schema_keywords(value)
            elif key == "allOf" and isinstance(value, list):
                # Recursively normalize allOf schemas
                normalized[key] = [
                    cls._normalize_schema_keywords(item) for item in value
                ]
            elif key == "anyOf" and isinstance(value, list):
                # Recursively normalize anyOf schemas
                normalized[key] = [
                    cls._normalize_schema_keywords(item) for item in value
                ]
            elif key == "oneOf" and isinstance(value, list):
                # Recursively normalize oneOf schemas
                normalized[key] = [
                    cls._normalize_schema_keywords(item) for item in value
                ]
            elif key == "not" and isinstance(value, dict):
                # Recursively normalize not schema
                normalized[key] = cls._normalize_schema_keywords(value)
            else:
                # Keep other values as-is
                normalized[key] = value

        return normalized

    # Fetches and caches GraphQL schema for a given function
    @classmethod
    def fetch_mcp_configuration(
        cls,
        partition_key: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetches and caches MCP configuration for a given endpoint.

        Args:
            partition_key: ID of the partition_key to fetch configuration from
            force_refresh: If True, bypass cache and fetch fresh data

        Returns:
            Dict containing the complete MCP configuration

        Raises:
            Exception: If GraphQL queries fail or data is malformed
        """
        # Check if configuration exists in cache and force_refresh is not requested
        if not force_refresh and cls.mcp_configuration.get(partition_key) is not None:
            return cls.mcp_configuration[partition_key]

        if cls.logger:
            cls.logger.info(
                f"Fetching MCP configuration for partition_key: {partition_key}"
            )

        try:
            # Step 1: Fetch all MCP functions
            response = _dispatch_internal_graphql(
                context={
                    "partition_key": partition_key,
                },
                query=MCP_FUNCTION_LIST,
                variables={},
            )
            response = Serializer.json_loads(response.get("body", response))

            if "data" in response:
                response = response.get("data", {})
            elif "errors" in response:
                import traceback

                cls.logger.error(
                    f"GraphQL errors in MCP_FUNCTION_LIST: {response['errors']}"
                )

                traceback.print_exc()
                raise Exception(f"Failed to fetch MCP functions: {response['errors']}")

            mcp_functions = response.get("mcpFunctionList", {}).get("mcpFunctionList")

            if not isinstance(mcp_functions, list) or len(mcp_functions) < 1:
                cls.logger.warning(
                    f"No MCP functions found for partition_key: {partition_key}"
                )

            # Step 2: Categorize functions by type
            tools: List[Dict[str, Any]] = []
            resources: List[Dict[str, Any]] = []
            prompts: List[Dict[str, Any]] = []

            for func in mcp_functions:
                if func.get("mcpType") == "tool":
                    # Normalize schema keywords in tool data
                    if "data" in func and isinstance(func["data"], dict):
                        func["data"]["inputSchema"] = cls._normalize_schema_keywords(
                            func["data"]["inputSchema"]
                        )
                    tools.append(func)
                elif func.get("mcpType") == "resource":
                    resources.append(func)
                elif func.get("mcpType") == "prompt":
                    prompts.append(func)
                else:
                    cls.logger.warning(
                        f"Unknown MCP function type: {func.get('mcpType')}"
                    )

            if cls.logger:
                cls.logger.info(
                    f"Found {len(tools)} tools, {len(resources)} resources, {len(prompts)} prompts"
                )

            # Step 3: Build initial configuration structure
            module_links = [
                cls._build_module_link(func)
                for func in mcp_functions
                if func.get("moduleName") and func.get("className")
            ]

            # Step 5: Cache the configuration
            cls.mcp_configuration[partition_key] = {
                "tools": [cls._build_function_config(tool) for tool in tools],
                "resources": [
                    cls._build_function_config(resource) for resource in resources
                ],
                "prompts": [cls._build_function_config(prompt) for prompt in prompts],
                "module_links": module_links,
                "modules": cls._fetch_modules_and_settings(
                    partition_key=partition_key,
                    module_links=module_links,
                ),
            }

            return cls.mcp_configuration[partition_key]

        except Exception as e:
            if cls.logger:
                cls.logger.error(
                    f"Failed to fetch MCP configuration for {partition_key}: {e}"
                )
            raise

    @classmethod
    def _build_function_config(cls, func: Dict[str, Any]) -> Dict[str, Any]:
        """Build function configuration with safe data extraction."""
        base_config = {
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "annotations": func.get("annotations", {}),
        }

        # Safely merge data field
        func_data = func.get("data", {})

        if isinstance(func_data, dict):
            base_config.update(func_data)

        return base_config

    @classmethod
    def _build_module_link(cls, func: Dict[str, Any]) -> Dict[str, Any]:
        """Build module link with proper field mapping."""
        module_link = {
            "type": func.get("mcpType", ""),  # Fixed: was "type" should be "mcpType"
            "name": func.get("name", ""),
            "module_name": func.get("moduleName", ""),
            "class_name": func.get("className", ""),
            "function_name": func.get("functionName", ""),
            "return_type": func.get("returnType", "text"),  # Default to "text"
        }

        # Include is_async if it's not None
        if func.get("isAsync") is not None:
            module_link["is_async"] = func.get("isAsync")

        return module_link

    @classmethod
    def _fetch_modules_and_settings(
        cls, partition_key: str, module_links: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fetch module and setting information efficiently."""
        modules_info = []
        # Group by module to reduce GraphQL calls
        modules_classes = {}

        for link in module_links:
            module_name = link.get("module_name")
            class_name = link.get("class_name")

            if not module_name or not class_name:
                if cls.logger:
                    cls.logger.warning(
                        f"Skipping module link with missing module_name or class_name: {link}"
                    )
                continue

            if module_name not in modules_classes:
                modules_classes[module_name] = set()
            modules_classes[module_name].add(class_name)

        # Process each module
        for module_name, class_names in modules_classes.items():
            try:
                # Fetch module information
                module_response = _dispatch_internal_graphql(
                    context={
                        "partition_key": partition_key,
                    },
                    query=MCP_MODULE,
                    variables={"moduleName": module_name},
                )
                module_response = Serializer.json_loads(
                    module_response.get("body", module_response)
                )

                if "errors" in module_response:
                    if cls.logger:
                        cls.logger.error(
                            f"Error fetching module {module_name}: {module_response['errors']}"
                        )
                    continue
                elif "data" in module_response:
                    module_response = module_response.get("data", {})

                module_data = module_response.get("mcpModule")

                if not module_data:
                    if cls.logger:
                        cls.logger.warning(f"No data found for module: {module_name}")
                    continue

                # Batch fetch settings for all classes in this module
                setting_ids = []
                class_to_setting_map = {}

                for class_name in class_names:
                    matching_class = next(
                        (
                            c
                            for c in module_data.get("classes", [])
                            if c.get("className") == class_name
                        ),
                        None,
                    )

                    if not matching_class:
                        if cls.logger:
                            cls.logger.warning(
                                f"Class '{class_name}' not found in module '{module_name}'"
                            )
                        continue

                    setting_id = matching_class.get("settingId")

                    if setting_id:
                        setting_ids.append(setting_id)
                        class_to_setting_map[class_name] = {
                            "setting_id": setting_id,
                            "class_info": matching_class,
                        }

                # Fetch settings (could be optimized further with batch query if available)
                for class_name, class_info in class_to_setting_map.items():
                    try:
                        setting_response = _dispatch_internal_graphql(
                            context={
                                "partition_key": partition_key,
                            },
                            query=MCP_SETTING,
                            variables={"settingId": class_info["setting_id"]},
                        )
                        setting_response = Serializer.json_loads(
                            setting_response.get("body", setting_response)
                        )

                        if "errors" in setting_response:
                            if cls.logger:
                                cls.logger.error(
                                    f"Error fetching setting {class_info['setting_id']}: {setting_response['errors']}"
                                )
                            setting_data = {}
                        else:
                            if "data" in setting_response:
                                setting_response = setting_response.get("data", {})

                            setting_data = setting_response.get("mcpSetting", {}).get(
                                "setting", {}
                            )

                        # Build module info
                        module_info = {
                            "module_name": module_name,
                            "package_name": module_data.get("packageName", module_name),
                            "class_name": class_name,
                            "setting": JSONSnakeCase.serialize(setting_data),
                            "source": module_data.get("source", ""),
                        }
                        modules_info.append(module_info)

                    except Exception as e:
                        Debugger.info(
                            variable=e, stage=f"{__name__}:_fetch_modules_and_settings"
                        )
                        if cls.logger:
                            cls.logger.error(
                                f"Error processing setting for {module_name}.{class_name}: {e}"
                            )
                        # Add module info with empty setting as fallback
                        module_info = {
                            "module_name": module_name,
                            "package_name": module_data.get("packageName", module_name),
                            "class_name": class_name,
                            "setting": {},
                            "source": module_data.get("source", ""),
                        }
                        modules_info.append(module_info)

            except Exception as e:
                Debugger.info(
                    variable=e, stage=f"{__name__}:_fetch_modules_and_settings"
                )
                if cls.logger:
                    cls.logger.error(f"Error processing module {module_name}: {e}")
                continue

        return modules_info

    @classmethod
    def refresh_mcp_configuration(cls, partition_key: str) -> Dict[str, Any]:
        """Force refresh of MCP configuration for an partition_key."""
        return cls.fetch_mcp_configuration(partition_key, force_refresh=True)

    @classmethod
    def clear_mcp_configuration_cache(cls, partition_key: str = None):
        """Clear MCP configuration cache for specific partition_key or all partition_keys."""
        if partition_key:
            cls.mcp_configuration.pop(partition_key, None)
            if cls.logger:
                cls.logger.info(
                    f"Cleared MCP configuration cache for partition_key: {partition_key}"
                )
        else:
            cls.mcp_configuration.clear()
            if cls.logger:
                cls.logger.info("Cleared all MCP configuration cache")

    @classmethod
    def get_logger(cls) -> logging.Logger:
        """Return the initialized application logger."""
        return cls.logger or logging.getLogger("mcp_daemon_engine")

    @classmethod
    def get_setting(cls) -> Dict[str, Any]:
        """Return the initialized application settings."""
        return cls.setting or {}

    @classmethod
    def get_cache_name(cls, module_type: str, model_name: str) -> str:
        """Generate standardized cache names."""
        base_name = cls.CACHE_NAMES.get(module_type, f"mcp_daemon_engine.{module_type}")
        return f"{base_name}.{model_name}"

    @classmethod
    def get_cache_ttl(cls) -> int:
        """Get the configured cache TTL."""
        return cls.CACHE_TTL

    @classmethod
    def is_cache_enabled(cls) -> bool:
        """Check if caching is enabled."""
        return cls.CACHE_ENABLED

    @classmethod
    def get_entity_children(cls, entity_type: str) -> List[Dict[str, Any]]:
        """Get child entities for a specific entity type (active backend)."""
        return cls.get_cache_relationships().get(entity_type, [])


def _dispatch_internal_graphql(**params: Dict[str, Any]) -> Any:
    """Run a GraphQL query/mutation against the daemon's own schema.

    Replaces the prior ``Config.mcp_core.mcp_daemon_graphql(...)`` self-loopback
    pattern. The import is function-scoped to avoid a circular load
    (``main.py`` imports ``handlers.config``).
    """
    from ..main import dispatch_graphql

    return dispatch_graphql(**params)
