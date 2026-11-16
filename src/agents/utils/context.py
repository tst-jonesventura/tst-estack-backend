import inspect

from pydantic_ai import RunContext
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, load_only, selectinload

import schemas.user_story_schemas
from agents.utils.agent_deps import AgentDeps
from database import models
from schemas import tech_spec_schemas
from utils import tag_utils

SESSION_ERROR_MSG = "Session is required in AgentDeps to fetch related items."
MAX_CONTEXT_ITEMS = 300
DONE_STORY_DESCRIPTION_CHARS = 300


def _format_tags(tags) -> str:
    return ", ".join(tag.tag for tag in tags) or "sem tags"


def _truncation_notice(rows: list, label: str) -> str:
    if len(rows) <= MAX_CONTEXT_ITEMS:
        return ""
    return (
        f"\n... a lista foi truncada nos {MAX_CONTEXT_ITEMS} {label} mais recentes. "
        "Use as tags e as ferramentas de busca por id para alcançar os demais."
    )


class ContextRegistry:
    @staticmethod
    def get_full_project_info(ctx: RunContext[AgentDeps]) -> str:
        """Fetch full project information for a given project ID."""
        with ctx.deps.db() as session:
            project = (
                session.query(models.Project)
                .options(joinedload(models.Project.repositories))
                .filter_by(id=ctx.deps.project_id)
                .first()
            )
            if not project:
                return "No project found with the given ID."

            info = (
                f"Project Name: {project.name}\nDescription: {project.description}\n\n"
            )
            info += "Repositories:\n"
            for repo in project.repositories:
                info += f"- {repo.url}\n"
            return info

    @staticmethod
    def get_stories_by_input_tags(ctx: RunContext[AgentDeps]) -> str:
        """Fetch related UserStories based on input tags."""
        if not ctx.deps.input_id:
            return "No input ID provided. Cannot fetch related user stories."
        if not ctx.deps.input_type:
            return "No input type provided. Cannot fetch related user stories."
        with ctx.deps.db() as session:
            related_stories = tag_utils.get_related_items_from_id(
                ctx.deps.input_id,
                ctx.deps.input_type,
                "user_story",
                session,
            )  # type: ignore

            if not related_stories:
                return "there are no related user stories on the project."
            # unzip List[Tuple[TaggableItem, float]] into List[TaggableItem] and List[float]
            related_stories, _ = zip(*related_stories) if related_stories else ([], [])

            related_stories_json = [
                schemas.user_story_schemas.UserStoryPublicWithTags.model_validate(
                    story
                ).model_dump_json(exclude={"timestamp", "project_id"})
                for story in related_stories
            ]
            done_count = sum(1 for s in related_stories if s.is_done)
            in_progress_count = len(related_stories) - done_count
            stories_str = (
                f"Especificações relacionadas no projeto ({done_count} concluída(s), "
                f"{in_progress_count} em andamento). "
                "Cite cada uma pelo título (campo name); o id completo serve só para "
                "chamar ferramentas ou preencher campos de id:\n"
            )

            stories_str += "\n".join(related_stories_json)
            return stories_str

    @staticmethod
    def get_tech_specs_by_input_tags(ctx: RunContext[AgentDeps]) -> str:
        """Fetch related DocumentTechSpecs based on input tags."""
        if not ctx.deps.input_id:
            return "No input ID provided. Cannot fetch related tech specs."

        if not ctx.deps.input_type:
            return "No input type provided. Cannot fetch related tech specs."

        with ctx.deps.db() as session:
            related_tech_specs = tag_utils.get_related_items_from_id(
                ctx.deps.input_id,
                ctx.deps.input_type,
                "tech_spec",
                session,
            )  # type: ignore
            if not related_tech_specs:
                return "there are no related tech specs on the project."

            # unzip List[Tuple[TaggableItem, float]] into List[TaggableItem] and List[float]
            related_tech_specs, _ = (
                zip(*related_tech_specs) if related_tech_specs else ([], [])
            )
            related_tech_specs_json = [
                tech_spec_schemas.DocumentTechSpecPublic.model_validate(
                    spec
                ).model_dump_json(exclude={"created_at", "project_id"})
                for spec in related_tech_specs
            ]

            tech_specs_str = "the related tech specs on the project are:\n"
            tech_specs_str += "\n".join(related_tech_specs_json)
            return tech_specs_str

    @staticmethod
    def get_all_specs(ctx: RunContext[AgentDeps]) -> str:
        """Fetch all UserStories (specs) in the project as id, name and tags.
        Use the tags to judge relevance to the subject being discussed before
        fetching full content with get_stories."""
        with ctx.deps.db() as session:
            stories = (
                session.query(models.UserStory)
                .filter_by(project_id=ctx.deps.project_id)
                .options(
                    load_only(models.UserStory.id, models.UserStory.name),
                    selectinload(models.UserStory.tags),
                )
                .order_by(models.UserStory.timestamp.desc())
                .limit(MAX_CONTEXT_ITEMS + 1)
                .all()
            )

            stories_str = (
                "As especificações (histórias) do projeto são as seguintes. Cite-as sempre "
                "pelo título; o id completo serve só para chamar ferramentas:\n"
            )
            stories_str += "\n".join(
                [
                    f'"{story.name}" (id para ferramentas: {story.id}) [tags: {_format_tags(story.tags)}]'
                    for story in stories[:MAX_CONTEXT_ITEMS]
                ]
            )
            stories_str += _truncation_notice(stories, "especificações")
            return stories_str

    @staticmethod
    def get_all_technical_details(ctx: RunContext[AgentDeps]) -> str:
        """Fetch all DocumentTechSpecs (technical details) in the project as id, title and tags.
        Use the tags to judge relevance to the subject being discussed before
        fetching full content with get_tech_specs."""
        with ctx.deps.db() as session:
            tech_specs = (
                session.query(models.DocumentTechSpec)
                .filter_by(project_id=ctx.deps.project_id)
                .options(
                    load_only(
                        models.DocumentTechSpec.id, models.DocumentTechSpec.title
                    ),
                    selectinload(models.DocumentTechSpec.tags),
                )
                .order_by(models.DocumentTechSpec.created_at.desc())
                .limit(MAX_CONTEXT_ITEMS + 1)
                .all()
            )

            tech_specs_str = (
                "Os detalhamentos técnicos do projeto são os seguintes. Cite-os sempre "
                "pelo título; o id completo serve só para chamar ferramentas:\n"
            )
            tech_specs_str += "\n".join(
                [
                    f'"{spec.title}" (id para ferramentas: {spec.id}) [tags: {_format_tags(spec.tags)}]'
                    for spec in tech_specs[:MAX_CONTEXT_ITEMS]
                ]
            )
            tech_specs_str += _truncation_notice(tech_specs, "detalhamentos técnicos")
            return tech_specs_str

    @staticmethod
    def list_project_tags(ctx: RunContext[AgentDeps]) -> str:
        """Fetch all Tags for a given project ID, sorted by usage count (most used first)."""
        with ctx.deps.db() as session:
            us_count = (
                session.query(
                    models.UserStoryTag.tag_id,
                    func.count().label("cnt"),
                )
                .group_by(models.UserStoryTag.tag_id)
                .subquery()
            )
            ts_count = (
                session.query(
                    models.TechSpecTag.tag_id,
                    func.count().label("cnt"),
                )
                .group_by(models.TechSpecTag.tag_id)
                .subquery()
            )
            tr_count = (
                session.query(
                    models.TranscriptionTag.tag_id,
                    func.count().label("cnt"),
                )
                .group_by(models.TranscriptionTag.tag_id)
                .subquery()
            )

            usage_expr = (
                func.coalesce(us_count.c.cnt, 0)
                + func.coalesce(ts_count.c.cnt, 0)
                + func.coalesce(tr_count.c.cnt, 0)
            ).label("usage_count")

            rows = (
                session.query(models.Tag.tag, usage_expr)
                .outerjoin(us_count, models.Tag.id == us_count.c.tag_id)
                .outerjoin(ts_count, models.Tag.id == ts_count.c.tag_id)
                .outerjoin(tr_count, models.Tag.id == tr_count.c.tag_id)
                .filter(models.Tag.project_id == ctx.deps.project_id)
                .order_by(usage_expr.desc())
                .all()
            )

            if not rows:
                return "there are currently no tags on the project."

            tags_str = (
                "the current tags on the project are (sorted by usage count — "
                "ALWAYS prefer reusing an existing tag over creating a new one):\n"
            )
            tags_str += "\n".join([f"{name} ({count} items)" for name, count in rows])
            return tags_str


    @staticmethod
    def get_done_stories_by_project(ctx: RunContext[AgentDeps]) -> str:
        """Fetch all completed (is_done=True) UserStories for a given project, including their names and descriptions."""
        with ctx.deps.db() as session:
            done_stories = (
                session.query(
                    models.UserStory.id,
                    models.UserStory.name,
                    func.substring(
                        models.UserStory.description, 1, DONE_STORY_DESCRIPTION_CHARS
                    ),
                )
                .filter_by(project_id=ctx.deps.project_id, is_done=True)
                .order_by(models.UserStory.timestamp.desc())
                .limit(MAX_CONTEXT_ITEMS + 1)
                .all()
            )
            if not done_stories:
                return "Não há especificações concluídas no projeto ainda."

            stories_str = (
                "Especificações já concluídas (is_done=True) no projeto. "
                "Estas representam funcionalidades já implementadas:\n"
            )
            stories_str += "\n".join(
                [
                    f'"{name}" (id para ferramentas: {story_id}): {description}'
                    for story_id, name, description in done_stories[:MAX_CONTEXT_ITEMS]
                ]
            )
            stories_str += _truncation_notice(done_stories, "especificações concluídas")
            return stories_str


_CONTEXT_DICT = inspect.getmembers(ContextRegistry, predicate=inspect.isfunction)
CONTEXT_DICT = dict(_CONTEXT_DICT)


def sync_context(session: Session):
    for name, context_fn in CONTEXT_DICT.items():
        context_entry = session.query(models.Context).filter_by(name=name).scalar()
        if context_entry:
            context_entry.name = name
            context_entry.description = (
                    context_fn.__doc__ or "No description available."
            )
        else:
            context_entry = models.Context(
                name=name,
                description=context_fn.__doc__ or "No description available.",
            )
        session.add(context_entry)