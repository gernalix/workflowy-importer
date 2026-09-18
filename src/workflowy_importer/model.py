from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass(slots=True)
class ImportNode:
    key: str
    name: str
    layout: str = "bullets"
    completed: bool = False
    source_file: str | None = None
    aliases: list[str] = field(default_factory=list)
    children: list["ImportNode"] = field(default_factory=list)

    def walk(self) -> Iterator["ImportNode"]:
        yield self
        for child in self.children:
            yield from child.walk()
