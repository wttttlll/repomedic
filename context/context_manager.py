
"""RepoMedic Context Manager：管理发送给模型的历史消息。"""

import json


class ContextManager:
    def __init__(
        self,
        max_turns: int = 4,
        max_chars: int = 40_000,
    ):
        if max_turns < 1 or max_chars < 1:
            raise ValueError("max_turns 和 max_chars 必须为正整数")

        self.max_turns = max_turns
        self.max_chars = max_chars

    @staticmethod
    def _chars(messages: list[dict]) -> int:
        """计算消息序列化后的字符数，不等于 Token 数。"""
        return len(json.dumps(messages, ensure_ascii=False))

    @staticmethod
    def _check_tool_pairs(turn: list[dict]) -> None:
        """检查 Tool Call 和 Tool Result 是否完整配对。"""
        pending: set[str] = set()

        for message in turn:
            role = message["role"]

            if role == "assistant":
                if pending:
                    raise ValueError("工具结果尚未返回，就出现下一条 assistant 消息")

                for call in message.get("tool_calls") or []:
                    call_id = call.get("id")

                    if (
                        not isinstance(call_id, str)
                        or not call_id
                        or call_id in pending
                    ):
                        raise ValueError("工具调用 ID 无效或重复")

                    pending.add(call_id)

            elif role == "tool":
                call_id = message.get("tool_call_id")

                if call_id not in pending:
                    raise ValueError("存在没有对应调用的 tool 消息")

                pending.remove(call_id)

        if pending:
            raise ValueError("工具调用缺少结果，暂不能发送给模型")

    def build(self, messages: list[dict]) -> list[dict]:
        """构建请求上下文，不修改 Session 的完整历史。"""

        if not isinstance(messages, list) or not messages:
            raise ValueError("消息历史不能为空")

        if (
            not isinstance(messages[0], dict)
            or messages[0].get("role") != "system"
        ):
            raise ValueError("第一条消息必须是 system")

        system = messages[0]

        # 1. 按用户轮次分组
        turns: list[list[dict]] = []

        for message in messages[1:]:
            if not isinstance(message, dict):
                raise ValueError("消息必须是字典")

            role = message.get("role")

            if role == "user":
                turns.append([message])

            elif role in ("assistant", "tool") and turns:
                turns[-1].append(message)

            else:
                raise ValueError("历史必须从 user 消息开始，""且只能包含 user/assistant/tool")

        if not turns:
            raise ValueError("至少需要一条用户消息")

        # 2. 确保工具调用和返回结果完整
        for turn in turns:
            self._check_tool_pairs(turn)

        # 3. 当前用户轮次必须完整保留
        kept = [turns[-1]]
        current_messages = [system] + turns[-1]

        if self._chars(current_messages) > self.max_chars:
            raise ValueError("当前轮次超过上下文预算，""请缩小任务或工具输出")

        # 4. 从近到远添加完整历史轮次
        for older in reversed(turns[:-1]):

            if len(kept) >= self.max_turns:
                break

            candidate_turns = [older] + kept

            candidate = [system] + [
                m
                for turn in candidate_turns
                for m in turn
            ]

            if self._chars(candidate) > self.max_chars:
                break

            kept = candidate_turns

        # 5. 返回筛选后的上下文
        return [system] + [
            m
            for turn in kept
            for m in turn
        ]