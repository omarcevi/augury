from rich.text import Text
from textual.widgets import Static

from augury.core.db.sources_repo import SourceRecord
from augury.tui.safe_text import text


class SourceDetail(Static):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text_content = ""

    def show(
        self,
        record: SourceRecord | None,
        count: int = 0,
        *,
        samples: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        if record is None:
            self.text_content = ""
            self.update(Text(""))
            return
        s = record.source
        lines = [
            s.name,
            f"id {s.id} · {s.origin} · added via {s.added_via} · {s.trust}",
            f"homepage {s.homepage or '—'}",
            f"recipe {s.recipe.model_dump_json()}",
            f"health {record.health} · {record.consecutive_failures} consecutive failures"
            f" · {count} items",
        ]
        if record.last_error:
            lines.append(f"last error {record.last_error}")
        if error:
            lines.append(f"test fetch failed: {error}")
        if samples is not None:
            lines.append(f"test fetch: {len(samples)} items")
            lines.extend(f"  · {title}" for title in samples[:3])
        lines.append("")
        lines.append("+ add · t test fetch · e enable/disable · d remove")
        self.text_content = "\n".join(lines)
        self.update(text(self.text_content))
