# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import Int, ObjectType


class McpConfigurationStats(ObjectType):
    tools = Int()
    resources = Int()
    prompts = Int()
    modules = Int()
    settings = Int()