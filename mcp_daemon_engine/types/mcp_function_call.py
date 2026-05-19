#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import DateTime, Int, List, ObjectType, String, Field
from silvaengine_dynamodb_base import ListObjectType
from silvaengine_utility import JSONCamelCase


class MCPFunctionCallType(ObjectType):
    partition_key = String()
    mcp_function_call_uuid = String()
    mcp_type = String()
    name = String()
    arguments = Field(JSONCamelCase)
    content = String()
    status = String()
    notes = String()
    time_spent = Int()
    updated_by = String()
    created_at = DateTime()
    updated_at = DateTime()


class MCPFunctionCallListType(ListObjectType):
    mcp_function_call_list = List(MCPFunctionCallType)
