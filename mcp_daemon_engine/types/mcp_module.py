#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import DateTime, List, ObjectType, String, Field
from silvaengine_dynamodb_base import ListObjectType
from silvaengine_utility import JSONCamelCase


class MCPModuleType(ObjectType):
    partition_key = String()
    module_name = String()
    package_name = String()
    classes = Field(JSONCamelCase)
    source = String()
    updated_by = String()
    created_at = DateTime()
    updated_at = DateTime()


class MCPModuleListType(ListObjectType):
    mcp_module_list = List(MCPModuleType)
