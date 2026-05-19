#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import DateTime, List, ObjectType, String, Field
from silvaengine_dynamodb_base import ListObjectType
from silvaengine_utility import JSONCamelCase


class MCPSettingType(ObjectType):
    partition_key = String()
    setting_id = String()
    setting = Field(JSONCamelCase)
    updated_by = String()
    created_at = DateTime()
    updated_at = DateTime()


class MCPSettingListType(ListObjectType):
    mcp_setting_list = List(MCPSettingType)
