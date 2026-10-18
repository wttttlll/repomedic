
"""本地 JSON Session 存储：创建、读取、原子保存。"""

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

class SessionManager:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        if (
            not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", session_id)
        ):
            raise ValueError("Session ID 格式不正确")
        return self.directory / f"{session_id}.json"

    def create(self, system_prompt: str) -> str:
        session_id = uuid.uuid4().hex
        self.save(session_id, [
            {"role": "system", "content": system_prompt}
        ])
        return session_id

    def load(self, session_id: str) -> list[dict]:
        path = self._file(session_id)

        if not path.is_file():
            raise FileNotFoundError(f"找不到 Session: {session_id}")

        data = json.loads(path.read_text(encoding="utf-8"))

        if (
            not isinstance(data, dict)
            or data.get("session_id") != session_id
            or not isinstance(data.get("messages"), list)
            or not all(
                isinstance(m, dict) for m in data["messages"]
            )
        ):
            raise ValueError("Session 文件内容损坏")

        return data["messages"]

    def save(self, session_id: str, messages: list[dict]) -> None:
        path = self._file(session_id)
        payload = {
            "session_id": session_id,
            "messages": messages
        }

        # 先写临时文件，再替换原文件
        # 避免写入中断导致 JSON 文件不完整
        tmp_name = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".session-",
                suffix=".tmp",
                delete=False
            ) as f:
                tmp_name = f.name
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_name, path)

        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)