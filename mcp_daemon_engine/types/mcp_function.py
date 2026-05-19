#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import Boolean, DateTime, Int, List, ObjectType, String, Field
from silvaengine_dynamodb_base import ListObjectType
from silvaengine_utility import JSONCamelCase


class MCPFunctionType(ObjectType):
    partition_key = String()
    name = String()
    mcp_type = String()
    description = String(name="description")
    data = Field(JSONCamelCase)
    annotations = String()
    module_name = String()
    class_name = String()
    function_name = String()
    return_type = String()
    is_async = Boolean()
    updated_by = String()
    created_at = DateTime()
    updated_at = DateTime()


class MCPFunctionListType(ListObjectType):
    mcp_function_list = List(MCPFunctionType)
