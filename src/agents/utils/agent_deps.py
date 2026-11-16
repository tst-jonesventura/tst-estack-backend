import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from agents.utils.agent_run_utils import AgentLogger, NullAgentLogger
from database.models import DocumentTechSpec, DocumentTranscription, UserStory
from fastapi import HTTPException
from sqlalchemy.orm import Session
from utils import tag_utils
from utils.tag_utils import TagItemTypes

SESSION_ERROR_MSG = "AgentDeps needs either a session or a session_factory."

# A safety net against runaway exploration, not the primary control. The counters
# live on the deps, which sub-agents share, so these cap a whole run rather than a
# single agent.
SOURCE_TOOL_BUDGET: dict[str, int] = {
    "get_file_contents": 40,
    "search_code": 10,
    "ask_git": 3,
    "ask_database": 3,
    "run_readonly_query": 15,
    "sample_rows": 10,
    "suggest_relationships": 5,
}


@dataclass
class AgentDeps:
    """Dependencies shared by an agent run and every tool and context it invokes.

    Agents either carry a long-lived ``session`` (the historical behaviour, still used by
    the pipeline agents) or a ``session_factory``, which lets a run that lasts minutes
    borrow a pooled connection only for the instants it actually queries. Always reach the
    database through :meth:`db` so both work.
    """

    project_id: str
    user_id: str
    session: Session | None = None
    session_factory: Callable[[], Session] | None = None
    input_id: str | None = None
    input_type: TagItemTypes | None = None
    agent_logger: AgentLogger = field(default_factory=NullAgentLogger)
    fetched_ids: set[str] = field(default_factory=set)
    used_tools: dict[str, int] = field(default_factory=dict)
    custom_usage_tool_limit: dict[str, int] = field(
        default_factory=lambda: dict(SOURCE_TOOL_BUDGET)
    )
    context_cache: dict[str, str] = field(default_factory=dict)
    file_cache: dict[str, object] = field(default_factory=dict)
    _usage_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _source_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False, compare=False)

    @contextmanager
    def db(self) -> Generator[Session]:
        """Yield a session, opening a short-lived one when the run owns no session."""
        if self.session is not None:
            yield self.session
            return
        if self.session_factory is None:
            raise ValueError(SESSION_ERROR_MSG)
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def can_use_tool(self, tool_name: str) -> bool:
        """Return True if tool_name usage is below limit."""
        limit = self.custom_usage_tool_limit.get(tool_name)
        if limit is None:
            return True

        return self.used_tools.get(tool_name, 0) < limit

    def tool_usage_limit_reached(self, tool_name: str) -> bool:
        """Return True if tool_name usage has reached the limit."""
        limit = self.custom_usage_tool_limit.get(tool_name)
        if limit is None:
            return False
        return self.used_tools.get(tool_name, 0) >= limit

    def record_tool_use(self, tool_name: str) -> None:
        """Increment usage for tool_name, raising if it would exceed limit."""
        with self._usage_lock:
            self.used_tools[tool_name] = self.used_tools.get(tool_name, 0) + 1

    def try_use_tool(self, tool_name: str) -> bool:
        """Count one use of tool_name unless its budget is spent; atomic across threads."""
        with self._usage_lock:
            if self.tool_usage_limit_reached(tool_name):
                return False
            self.used_tools[tool_name] = self.used_tools.get(tool_name, 0) + 1
            return True

    @contextmanager
    def source_turn(self, source: str) -> Generator[None]:
        """One call at a time per knowledge source, while different sources run in parallel."""
        with self._usage_lock:
            lock = self._source_locks.setdefault(source, threading.Lock())
        with lock:
            yield

    def get_input(
        self, session: Session | None = None, clear_tags: bool = False
    ) -> tuple[
        DocumentTranscription | DocumentTechSpec | UserStory,
        str,
    ]:
        if not self.input_id or not self.input_type:
            raise ValueError("input_id and input_type must be provided to fetch input")
        if session is not None:
            return self._load_input(session, clear_tags)
        with self.db() as scoped:
            return self._load_input(scoped, clear_tags)

    def _load_input(self, session: Session, clear_tags: bool):
        if not self.input_id or not self.input_type:
            raise ValueError("input_id and input_type must be provided to load input")
        model = tag_utils.TAG_MODEL_MAP[self.input_type]
        schema = tag_utils.TAG_SCHEMA_MAP[self.input_type]
        item = session.query(model).filter_by(id=self.input_id).one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
        if clear_tags:
            item.tags = []
        item_content = schema.model_validate(item).model_dump_json()
        return item, item_content
