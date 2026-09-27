"""Replaying a discovery run from sessions.db (spec §5.6): one line per model turn, tool call
and tool result, for `augury dev discovery <run-id>`."""

import json
from pathlib import Path

from google.adk.sessions.sqlite_session_service import SqliteSessionService

from augury.agents.discovery.agent import APP_NAME, USER_ID
from augury.core.text import strip_control_chars

LINE_CHARS = 200


def _line(text: str) -> str:
    return " ".join(strip_control_chars(text).split())[:LINE_CHARS]


async def transcript(sessions_db: Path, session_id: str) -> list[str] | None:
    """None when sessions.db has no such run."""
    if not sessions_db.exists():
        return None
    service = SqliteSessionService(db_path=str(sessions_db))
    session = await service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if session is None:
        return None
    lines: list[str] = []
    for event in session.events:
        for part in (event.content.parts or []) if event.content else []:
            if part.function_call:
                args = json.dumps(part.function_call.args or {}, ensure_ascii=False)
                lines.append(_line(f"→ {part.function_call.name} {args}"))
            elif part.function_response:
                result = json.dumps(part.function_response.response or {}, ensure_ascii=False)
                lines.append(_line(f"← {part.function_response.name} {result}"))
            elif part.text and not part.thought:
                lines.append(_line(f"{event.author}: {part.text}"))
    return lines
