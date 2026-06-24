#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import base64
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo

from .config import Config


def load_mcp_configuration_into_models(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Load MCP configuration JSON into database models.
    This is the reverse of Config.fetch_mcp_configuration().

    Args:
        info (ResolveInfo): GraphQL ResolveInfo object for database operations
        kwargs (Dict[str, Any]): Dictionary containing:
            - mcp_configuration: Complete MCP configuration dictionary
            - updated_by: User identifier for update tracking
            - partition_key: Endpoint ID to load configuration for (from info.context)

    Returns:
        Dict[str, Any]: Dictionary with statistics about loaded items containing:
            - tools: Number of tools loaded
            - resources: Number of resources loaded
            - prompts: Number of prompts loaded
            - modules: Number of modules loaded
            - settings: Number of settings loaded

    Raises:
        Exception: If loading fails
    """
    try:
        from ..models.repositories import get_repo
        from .mcp_utility import get_mcp_configuration_by_module

        partition_key = info.context["partition_key"]
        info.context["logger"].info(
            f"Loading MCP configuration for endpoint: {partition_key}"
        )
        updated_by = kwargs["updated_by"]

        mcp_configuration = None
        if "mcp_configuration" in kwargs:
            mcp_configuration = kwargs["mcp_configuration"]
        elif "module_name" in kwargs:
            mcp_configuration = get_mcp_configuration_by_module(
                kwargs.get("package_name"),
                kwargs["module_name"],
                source=kwargs.get("source"),
            )
            info.context["logger"].info(
                f"Loading MCP configuration for package: {kwargs.get('package_name', '')}, module: {kwargs['module_name']}"
            )
        else:
            raise Exception("No MCP configuration provided")

        stats = {"tools": 0, "resources": 0, "prompts": 0, "modules": 0, "settings": 0}

        # Load tools
        if "tools" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['tools'])} tools"
            )
            for tool in mcp_configuration["tools"]:
                tool_data = {
                    "partition_key": partition_key,
                    "name": tool.get("name"),
                    "mcp_type": "tool",
                    "description": tool.get("description"),
                    "data": {
                        k: v
                        for k, v in tool.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": tool.get("annotations"),
                    "is_async": tool.get("is_async", False),
                    "updated_by": updated_by,
                }
                info.context["logger"].info(
                    f"Loading tool '{tool.get('name')}' with data: {tool_data['data']}"
                )
                get_repo("mcp_function").insert_update(info, **tool_data)
                stats["tools"] += 1

        # Load resources
        if "resources" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['resources'])} resources"
            )
            for resource in mcp_configuration["resources"]:
                resource_data = {
                    "partition_key": partition_key,
                    "name": resource.get("name"),
                    "mcp_type": "resource",
                    "description": resource.get("description"),
                    "data": {
                        k: v
                        for k, v in resource.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": resource.get("annotations"),
                    "is_async": resource.get("is_async", False),
                    "updated_by": updated_by,
                }
                get_repo("mcp_function").insert_update(info, **resource_data)
                stats["resources"] += 1

        # Load prompts
        if "prompts" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['prompts'])} prompts"
            )
            for prompt in mcp_configuration["prompts"]:
                prompt_data = {
                    "partition_key": partition_key,
                    "name": prompt.get("name"),
                    "mcp_type": "prompt",
                    "description": prompt.get("description"),
                    "data": {
                        k: v
                        for k, v in prompt.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": prompt.get("annotations"),
                    "is_async": prompt.get("is_async", False),
                    "updated_by": updated_by,
                }
                get_repo("mcp_function").insert_update(info, **prompt_data)
                stats["prompts"] += 1

        # Load module links as functions with module information
        if "module_links" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['module_links'])} module links"
            )
            for link in mcp_configuration["module_links"]:
                # Only update the module-related fields, don't overwrite existing data
                link_data = {
                    "partition_key": partition_key,
                    "name": link.get("name"),
                    "mcp_type": link.get("type", "tool"),
                    "module_name": link.get("module_name"),
                    "class_name": link.get("class_name"),
                    "function_name": link.get("function_name"),
                    "return_type": link.get("return_type", "text"),
                    "is_async": link.get("is_async", False),
                    "updated_by": updated_by,
                    # Don't include 'data' field to avoid overwriting existing data
                }
                get_repo("mcp_function").insert_update(info, **link_data)

        # Load modules
        if "modules" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['modules'])} modules"
            )

            # Aggregate all settings from all modules and create one shared setting
            setting_insert_data = {
                "partition_key": partition_key,
                "setting": {},
                "updated_by": updated_by,
            }

            # Aggregate all module settings
            for module in mcp_configuration["modules"]:
                setting_insert_data["setting"] = dict(
                    setting_insert_data["setting"], **module.get("setting", {})
                )

            # Apply Config.setting overrides (after aggregating all module settings)
            from .config import Config

            for k, v in Config.setting.items():
                if k in setting_insert_data["setting"].keys():
                    setting_insert_data["setting"][k] = v

            # Apply variables overrides (highest priority)
            if "variables" in kwargs:
                for k, v in kwargs["variables"].items():
                    if k in setting_insert_data["setting"].keys():
                        setting_insert_data["setting"][k] = v

            # Create the shared setting and get the setting_id from the result.
            # The DynamoDB repo returns a typed object (.setting_id); the
            # PostgreSQL repo returns a normalized dict (["setting_id"]).
            mcp_setting = get_repo("mcp_setting").insert_update(info, **setting_insert_data)
            setting_id = (
                mcp_setting["setting_id"]
                if isinstance(mcp_setting, dict)
                else mcp_setting.setting_id
            )
            stats["settings"] += 1

            for module in mcp_configuration["modules"]:
                # Create module with class information
                classes = [
                    {"class_name": module.get("class_name"), "setting_id": setting_id}
                ]

                module_data = {
                    "partition_key": partition_key,
                    "module_name": module.get("module_name"),
                    "package_name": module.get(
                        "package_name", module.get("module_name")
                    ),
                    "classes": classes,
                    "source": module.get("source", kwargs.get("source", "")),
                    "updated_by": updated_by,
                }
                get_repo("mcp_module").insert_update(info, **module_data)
                stats["modules"] += 1

        info.context["logger"].info(f"Successfully loaded MCP configuration: {stats}")
        return stats

    except Exception as e:
        log = traceback.format_exc()
        info.context["logger"].error(f"Failed to load MCP configuration: {log}")
        raise e


_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _validate_package_name(package_name: str) -> None:
    if not package_name or not _PACKAGE_NAME_RE.match(package_name):
        raise Exception(
            f"Invalid package name '{package_name}': must match ^[A-Za-z][A-Za-z0-9_]*$"
        )


def generate_upload_url(package_name: str, logger: Any = None) -> Dict[str, Any]:
    """Generate a presigned S3 PUT URL for uploading an MCP package.

    The S3 key follows the canonical convention used by
    ``_download_and_extract_package``: ``{package_name}.zip``.

    Args:
        package_name: Logical package name (becomes the S3 key).
        logger: Optional logger; falls back to ``Config.logger``.

    Returns:
        Dict with ``upload_url``, ``s3_key``, and ``expires_at``.
    """
    log = logger or Config.logger

    _validate_package_name(package_name)

    if not Config.funct_bucket_name:
        raise Exception("S3 bucket not configured (FUNCT_BUCKET_NAME is missing)")

    s3_key = f"{package_name}.zip"
    log.info(
        f"Generating presigned PUT URL for s3://{Config.funct_bucket_name}/{s3_key}"
    )

    url = Config.aws_s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": Config.funct_bucket_name,
            "Key": s3_key,
            "ContentType": "application/zip",
        },
        ExpiresIn=900,
    )

    return {
        "upload_url": url,
        "s3_key": s3_key,
        "expires_at": pendulum.now("UTC").add(minutes=15),
    }


def validate_manifest(
    mcp_configuration: Dict[str, Any],
    logger: Any = None,
    module_name: str = None,
) -> None:
    """Validate an MCP configuration manifest before persisting to DB.

    Rules:
        1. At least one of ``tools``, ``resources``, ``prompts`` must exist and be non-empty.
        2. Every ``module_links[*].name`` must match a record in ``tools``, ``resources``, or ``prompts``.
        3. Every ``modules[*].module_name`` must appear in at least one ``module_links[*].module_name``.
        4. ``modules[*].class_name`` must be non-empty.
        5. ``module_links[*].type`` must match the referenced function type.
        6. If supplied, ``module_name`` must match every manifest module name.
        7. ``mcp_configuration`` must be a dict.

    Raises:
        Exception: On validation failure with a descriptive message.
    """
    log = logger or Config.logger

    if not isinstance(mcp_configuration, dict):
        raise Exception("mcp_configuration must be a dict")

    function_types = {}

    for key, mcp_type in (
        ("tools", "tool"),
        ("resources", "resource"),
        ("prompts", "prompt"),
    ):
        items = mcp_configuration.get(key)
        if items is not None:
            if not isinstance(items, list):
                raise Exception(f"'{key}' must be a list")
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if name:
                    function_types[name] = mcp_type

    if not function_types:
        raise Exception(
            "At least one of tools, resources, or prompts must be present and contain items with a 'name' field"
        )

    module_links = mcp_configuration.get("module_links", [])
    if module_links:
        for link in module_links:
            if not isinstance(link, dict):
                continue
            link_name = link.get("name")
            link_type = link.get("type", "tool")
            if link_name and link_name not in function_types:
                raise Exception(
                    f"module_links references '{link_name}' but it does not exist in tools, resources, or prompts"
                )
            if link_name and link_type != function_types[link_name]:
                raise Exception(
                    f"module_links entry '{link_name}' has type '{link_type}' "
                    f"but the referenced function is '{function_types[link_name]}'"
                )
            for required_key in ("module_name", "class_name", "function_name"):
                if not link.get(required_key):
                    raise Exception(
                        f"module_links entry '{link_name}' must have a '{required_key}'"
                    )

    modules = mcp_configuration.get("modules", [])
    if modules:
        link_module_names = set()
        for link in module_links:
            if isinstance(link, dict) and link.get("module_name"):
                link_module_names.add(link["module_name"])

        for module in modules:
            if not isinstance(module, dict):
                continue
            mod_name = module.get("module_name")
            cls_name = module.get("class_name")
            if not mod_name:
                raise Exception("Each module must have a 'module_name'")
            if not cls_name:
                raise Exception(f"Module '{mod_name}' must have a 'class_name'")
            if module_name and mod_name != module_name:
                raise Exception(
                    f"Manifest module '{mod_name}' does not match requested module_name '{module_name}'"
                )
            if link_module_names and mod_name not in link_module_names:
                raise Exception(
                    f"Module '{mod_name}' is not referenced by any module_links entry"
                )

    if log:
        log.info("Manifest validation passed")


def _import_mcp_configuration_from_dir(
    extract_dir: str, module_name: str, log: Any
) -> Dict[str, Any]:
    """Import ``module_name`` from ``extract_dir`` and return its
    ``MCP_CONFIGURATION`` attribute.

    Used as a fallback when an uploaded package has no
    ``mcp_configuration.json`` at the archive root. The import is performed
    in isolation: ``sys.path`` is prepended with ``extract_dir`` and any
    previously cached version of the module (and its submodules) is removed
    so the import resolves against the temp tree. State is restored in a
    ``finally`` block whether the import succeeds or not.
    """
    original_path = sys.path[:]
    cached_modules = {
        k: v
        for k, v in sys.modules.items()
        if k == module_name or k.startswith(f"{module_name}.")
    }

    try:
        sys.path.insert(0, extract_dir)
        for cached in list(cached_modules.keys()):
            del sys.modules[cached]

        module = importlib.import_module(module_name)
        if not hasattr(module, "MCP_CONFIGURATION"):
            raise Exception(
                f"Package has no mcp_configuration.json at archive root and "
                f"module '{module_name}' does not expose MCP_CONFIGURATION"
            )
        return getattr(module, "MCP_CONFIGURATION")
    finally:
        sys.path[:] = original_path
        for mod_name in list(sys.modules.keys()):
            if mod_name == module_name or mod_name.startswith(f"{module_name}."):
                del sys.modules[mod_name]
        for k, v in cached_modules.items():
            sys.modules[k] = v


def download_and_validate_zip(
    s3_key: str,
    package_name: str,
    module_name: str,
    logger: Any = None,
) -> Dict[str, Any]:
    """Download a ZIP from S3, validate its manifest, and stage for runtime.

    Steps:
        1. Download ZIP from S3 to a temp directory.
        2. Extract and verify ``mcp_configuration.json`` exists and is valid JSON.
        3. Validate the manifest using ``validate_manifest``.
        4. Copy ZIP to the canonical function path and extract there for runtime use.
        5. Return the parsed manifest dict.

    Args:
        s3_key: S3 key of the uploaded ZIP (e.g. ``my_pkg.zip``).
        package_name: Package name (used for the local zip filename).
        module_name: Module name inside the package.
        logger: Optional logger.

    Returns:
        Parsed ``mcp_configuration.json`` as a dict.
    """
    log = logger or Config.logger

    _validate_package_name(package_name)
    _validate_package_name(module_name)

    if not Config.funct_bucket_name:
        raise Exception("S3 bucket not configured (FUNCT_BUCKET_NAME is missing)")

    canonical_s3_key = f"{package_name}.zip"
    if s3_key != canonical_s3_key:
        raise Exception(
            f"Invalid S3 key '{s3_key}': expected canonical key '{canonical_s3_key}'"
        )

    tmp_dir = tempfile.mkdtemp(prefix="mcp_upload_")
    try:
        zip_path = os.path.join(tmp_dir, s3_key)
        log.info(f"Downloading s3://{Config.funct_bucket_name}/{s3_key} to {zip_path}")
        Config.aws_s3.download_file(Config.funct_bucket_name, s3_key, zip_path)

        if not zipfile.is_zipfile(zip_path):
            raise Exception(f"Downloaded file {s3_key} is not a valid ZIP archive")

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info_z in zf.infolist():
                name = info_z.filename
                normalized_name = name.replace("\\", "/")
                if (
                    normalized_name.startswith("/")
                    or ":" in normalized_name.split("/")[0]
                    or ".." in normalized_name.split("/")
                ):
                    raise Exception(
                        f"ZIP contains unsafe path: {name} (absolute or '..' traversal)"
                    )
            zf.extractall(extract_dir)

        # Importable-root check must run before the import fallback can succeed.
        module_root_dir = os.path.join(extract_dir, module_name)
        module_root_file = os.path.join(extract_dir, f"{module_name}.py")
        if not os.path.isdir(module_root_dir) and not os.path.isfile(module_root_file):
            raise Exception(
                f"Package does not expose '{module_name}' as an importable root "
                f"(expected {module_name}/__init__.py or {module_name}.py)"
            )

        # Manifest source: prefer mcp_configuration.json at archive root, fall
        # back to importing {module_name}.MCP_CONFIGURATION from the temp tree.
        manifest_path = os.path.join(extract_dir, "mcp_configuration.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                mcp_configuration = json.load(f)
            log.info("Loaded manifest from mcp_configuration.json")
        else:
            mcp_configuration = _import_mcp_configuration_from_dir(
                extract_dir, module_name, log
            )
            log.info(
                "Loaded manifest from module.MCP_CONFIGURATION "
                "(no mcp_configuration.json at archive root)"
            )

        validate_manifest(mcp_configuration, logger=log, module_name=module_name)

        canonical_zip = os.path.join(Config.funct_zip_path, f"{package_name}.zip")
        shutil.copy2(zip_path, canonical_zip)
        log.info(f"Copied ZIP to canonical path: {canonical_zip}")

        with zipfile.ZipFile(canonical_zip, "r") as zf:
            zf.extractall(Config.funct_extract_path)
        log.info(f"Extracted package to {Config.funct_extract_path}")

        from .mcp_utility import purge_module_import_cache

        purge_module_import_cache(module_name)
        log.info(f"Cleared Python import cache for module: {module_name}")

        return mcp_configuration

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_mcp_package(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Download a staged ZIP from S3, validate, and persist MCP configuration to DB.

    Orchestrates the full upload-processing flow:
        1. Download ZIP from S3 via ``download_and_validate_zip``.
        2. Clear the MCP configuration cache for the partition.
        3. Persist entities to DynamoDB via ``load_mcp_configuration_into_models``.
        4. Warm the configuration cache.

    Args:
        info: GraphQL ResolveInfo (carries ``context`` with ``partition_key`` and ``logger``).
        **kwargs: Must include ``s3_key``, ``module_name``, ``package_name``, ``updated_by``.
                  Optional: ``source`` (defaults to ``s3``), ``variables``.

    Returns:
        Dict with ``tools``, ``resources``, ``prompts``, ``modules``, ``settings`` counts.
    """
    partition_key = info.context["partition_key"]
    s3_key = kwargs["s3_key"]
    module_name = kwargs["module_name"]
    package_name = kwargs["package_name"]
    source = kwargs.get("source", "s3")
    variables = kwargs.get("variables")
    updated_by = kwargs["updated_by"]

    logger = info.context.get("logger") or Config.logger

    _validate_package_name(package_name)
    _validate_package_name(module_name)

    mcp_configuration = download_and_validate_zip(
        s3_key=s3_key,
        package_name=package_name,
        module_name=module_name,
        logger=logger,
    )

    Config.clear_mcp_configuration_cache(partition_key)

    load_kwargs = {
        "mcp_configuration": mcp_configuration,
        "module_name": module_name,
        "package_name": package_name,
        "source": source,
        "updated_by": updated_by,
    }
    if variables:
        load_kwargs["variables"] = variables

    stats = load_mcp_configuration_into_models(info, **load_kwargs)

    try:
        Config.fetch_mcp_configuration(partition_key, force_refresh=True)
        logger.info(f"Cache warmed for partition_key: {partition_key}")
    except Exception as e:
        logger.warning(f"Cache warm failed for {partition_key}: {e}")

    return stats


def process_base64_package(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Decode a Base64-encoded ZIP, upload to S3, then process using the same flow.

    Args:
        info: GraphQL ResolveInfo.
        **kwargs: Must include ``package_base64``, ``package_name``, ``module_name``,
                  ``updated_by``. Optional: ``source``, ``variables``.

    Returns:
        Dict with ``tools``, ``resources``, ``prompts``, ``modules``, ``settings`` counts.
    """
    logger = info.context.get("logger") or Config.logger

    package_base64 = kwargs["package_base64"]
    package_name = kwargs["package_name"]
    module_name = kwargs["module_name"]
    source = kwargs.get("source", "s3")

    _validate_package_name(package_name)
    _validate_package_name(module_name)

    if not Config.funct_bucket_name:
        raise Exception("S3 bucket not configured (FUNCT_BUCKET_NAME is missing)")

    raw_bytes = base64.b64decode(package_base64)

    s3_key = f"{package_name}.zip"
    logger.info(
        f"Uploading Base64-decoded package to s3://{Config.funct_bucket_name}/{s3_key} "
        f"({len(raw_bytes)} bytes)"
    )

    from io import BytesIO

    Config.aws_s3.upload_fileobj(
        BytesIO(raw_bytes),
        Config.funct_bucket_name,
        s3_key,
        ExtraArgs={"ContentType": "application/zip"},
    )

    process_kwargs = {
        "s3_key": s3_key,
        "module_name": module_name,
        "package_name": package_name,
        "source": source,
        "updated_by": kwargs["updated_by"],
    }
    if "variables" in kwargs:
        process_kwargs["variables"] = kwargs["variables"]

    return process_mcp_package(info, **process_kwargs)
