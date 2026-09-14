"""Review queues survive navigation for the lifetime of the desktop process."""
from dataclasses import dataclass, field
import secrets

@dataclass
class DecisionSession:
    id: str
    section: str
    queue: list[int]
    index: int = 0
    counts: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

sessions: dict[str, DecisionSession] = {}


def create(section, queue):
    session=DecisionSession(secrets.token_urlsafe(24),section,queue)
    sessions[session.id]=session
    while len(sessions)>20:
        sessions.pop(next(iter(sessions)))
    return session
