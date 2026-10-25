"""OpenAI 兼容 Function Calling 的工具 Schema。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出 demo_repo 根目录中的文件和文件夹",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 demo_repo 内指定路径的 UTF-8 文本文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对路径，例如 calc.py",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "运行 demo_repo 中预先定义的 Python 单元测试，"
                "返回测试结果及失败原因。不接受参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "只有获得写入授权时才能修改 demo_repo/calc.py。"
                "不能修改测试文件和其他文件。修改后由 Python 自动运行单元测试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "只能填写 calc.py",
                    },
                    "content": {
                        "type": "string",
                        "description": "修改后的完整文件内容",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_test_analysis",
            "description": (
                "将代码测试分析任务委派给独立的 Test Analyst "
                "子 Agent。子 Agent 具有独立上下文，"
                "只能读取代码和运行测试，不能修改文件。"
                "适用于分析测试失败、定位 Bug、"
                "检查测试覆盖情况。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "交给子 Agent 的具体任务，"
                            "例如：运行测试并分析 calc.py 的问题"
                        ),
                    }
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_ppt_template",
            "description": "列出或检查 templates/ppt 中的 PPTX 模板，返回页数和版式名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "description": "模板文件名，如 company.pptx；省略时列出模板"
                    }
                },
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_ppt",
            "description": "创建高质量可编辑 PPTX，可用自定义模板、版式、图表和预览。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "输出文件名，如 report.pptx"
                    },
                    "template": {
                        "type": "string",
                        "description": "可选的模板文件名"
                    },
                    "template_mode": {
                        "type": "string",
                        "enum": [
                                "replace",
                                "append",
                                "reference_fill"
                            ],
                            "description":
                                "reference_fill 使用固定底板和参考样式；replace 重建幻灯片；append 追加页面"
                    },
                    "palette": {
                        "type": "string",
                        "enum": ["ocean", "forest", "sunset", "minimal","academic_navy"]
                    },
                    "preview": {
                        "type": "boolean"
                    },
                    "slides": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {
                            "type": "object",
                            "properties": {
                                "layout": {
                                    "type": "string",
                                    "enum": [
                                        "cover", "section", "bullets",
                                        "two_column", "cards", "quote",
                                        "chart", "timeline","outline","table",
                                        "auto","info_blocks","two_column",
                                        "cover","outline","cards",
                                    ]
                                },
                                "template_layout": {"type": "string"},
                                "title": {"type": "string"},
                                "subtitle": {"type": "string"},
                                "bullets": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "left": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "right": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "cards": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "body": {"type": "string"},
                                            "table": {
                                                "type": "object",
                                                "properties": {
                                                    "headers": {
                                                        "type": "array",
                                                        "items": {"type": "string"}
                                                    },
                                                    "rows": {
                                                        "type": "array",
                                                        "items": {
                                                            "type": "array",
                                                            "items": {"type": "string"}
                                                        }
                                                    }
                                                },
                                                "required": ["headers", "rows"],
                                                "additionalProperties": False
                                            },
                                        }
                                    }
                                },
                                "quote": {"type": "string"},
                                "chart": {"type": "object"},
                                "events": {
                                    "type": "array",
                                    "items": {"type": "object"}
                                },
                            },
                            "required": ["title"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["filename", "slides"],
                "additionalProperties": False
            }
        }
    },
]
def validate_tools(tools):
    names = set()

    for i, tool in enumerate(tools):
        if tool.get("type") != "function":
            raise ValueError(
                f"Invalid tool type at index {i}"
            )

        function = tool.get("function")

        if not isinstance(function, dict):
            raise ValueError(
                f"Missing function at index {i}"
            )

        name = function.get("name")

        if not name:
            raise ValueError(
                f"Missing tool name at index {i}"
            )

        if name in names:
            raise ValueError(
                f"Duplicate tool name: {name}"
            )

        names.add(name)


validate_tools(TOOLS)


def _configure_reference_fill_v4():
    entry = next(t["function"] for t in TOOLS
                 if t.get("type") == "function"
                 and t["function"].get("name") == "generate_ppt")
    props = entry["parameters"]["properties"]
    props.setdefault("template_mode", {"type":"string"})["enum"] = [
        "replace", "append", "reference_fill"]
    props.setdefault("palette", {"type":"string"})["enum"] = [
        "ocean", "forest", "sunset", "minimal", "academic_navy"]
    slide_props = props["slides"]["items"]["properties"]
    slide_props.setdefault("layout", {"type":"string"})["enum"] = [
        "auto", "cover", "outline", "section", "bullets",
        "two_column", "cards", "quote", "chart", "timeline", "table", "info_blocks"]
    slide_props["table"] = {
        "type":"object",
        "properties":{
            "headers":{"type":"array","items":{"type":"string"}},
            "rows":{"type":"array","items":{
                "type":"array","items":{"type":"string"}}}
        },"required":["headers","rows"],"additionalProperties":False}
    slide_props["blocks"] = {
        "type":"array","minItems":1,"maxItems":4,
        "items":{"type":"object","properties":{
            "title":{"type":"string"},
            "body":{"type":"string"},
            "bullets":{"type":"array","items":{"type":"string"}}
        },"required":["title"],"additionalProperties":False}}

_configure_reference_fill_v4()
