from contextlib import contextmanager

class HookDenied(PermissionError):
    """工具调用被 Hook拒绝。"""

class HookManager:
    def __init__(self, logger=print):
        self.logger = logger

    def pre_tool_use(self, name, arguments, *, allow_write):
        """工具执行前：检查权限。"""

        if name == "write_file":
            if not allow_write:
                self.logger("[PreToolUse] write_file: DENY")
                raise HookDenied("当前会话未授权写入")

            if arguments.get("path") != "calc.py":
                self.logger("[PreToolUse] write_file: DENY")
                raise HookDenied("只允许修改 calc.py")

        if name == "generate_ppt":
            if not allow_write:
                self.logger("[PreToolUse] generate_ppt: DENY")
                raise HookDenied("当前会话没有 PPT 写入权限")

        self.logger(f"[PreToolUse] {name}: ALLOW")

    def post_tool_use(self, name, *, success):
        """工具执行后：记录结果，不记录文件内容。"""

        status = "OK" if success else "ERROR"
        self.logger(f"[PostToolUse] {name}: {status}")

    def on_verification_fail(self, status):
        """测试失败或验证环境异常时记录事件。"""

        if status not in {"FAIL", "ERROR", "TIMEOUT"}:
            raise ValueError("不是有效的验证失败状态")

        self.logger(f"[OnVerificationFail] {status}")


    @contextmanager
    def guard(self, name, arguments, *, allow_write):
        """
        统一管理工具执行前后的 Hook。
        PreToolUse:
            执行工具前检查权限。
        PostToolUse:
            工具结束后记录成功或异常。
        """
        # 1. 执行前检查
        self.pre_tool_use(name,arguments,allow_write=allow_write)

        try:
            # 2. 执行真实工具
            yield

        except Exception:
            # 3. 工具执行发生异常
            self.post_tool_use(name,success=False)
            raise

        else:
            # 4. 工具正常返回
            self.post_tool_use(name,success=True)