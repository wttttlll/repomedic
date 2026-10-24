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
            "name": "generate_ppt",
            "description": "根据幻灯片内容生成可编辑的 PPTX 文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "输出文件名，如 demo.pptx"
                    },
                    "slides": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "bullets": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                }
                            },
                            "required": ["title", "bullets"]
                        }
                    }
                },
                "required": ["filename", "slides"]
            }
        }
    }
]
