from dataclasses import dataclass, field


@dataclass
class Books:
    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
