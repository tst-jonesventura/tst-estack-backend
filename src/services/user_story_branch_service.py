from typing import List
from sqlalchemy.orm import Session

from agents.utils.agent_deps import AgentDeps
from database.models import UserStoryBranch, ProjectRepository
from schemas.user_story_schemas import UserStoryBranchCreate, CodeGitAgentOutput
from utils import log

logger = log.setup_custom_logger(__name__)


class UserStoryBranchService:
    def __init__(self):
        # Dont need to initialize anything for now
        pass

    def create_branch(self, session: Session, branch_in: UserStoryBranchCreate) -> UserStoryBranch:
        db_branch = UserStoryBranch(
            branch_name=branch_in.branch_name,
            user_story_id=branch_in.user_story_id,
            project_repository_id=branch_in.project_repository_id
        )
        session.add(db_branch)
        session.commit()
        session.refresh(db_branch)
        return db_branch

    def get_branch(self, session: Session, branch_id: str) -> UserStoryBranch | None:
        return session.query(UserStoryBranch).filter(UserStoryBranch.id == branch_id).first()

    def list_branches_by_user_story(self, session: Session, user_story_id: str) -> List[UserStoryBranch]:
        return session.query(UserStoryBranch).filter(UserStoryBranch.user_story_id == user_story_id).all()

    def delete_branch(self, session: Session, branch_id: str) -> bool:
        db_branch = self.get_branch(session, branch_id)
        if db_branch:
            session.delete(db_branch)
            session.commit()
            return True
        return False

    def save_branch_from_agent_run(self, code_git_output: CodeGitAgentOutput, deps: AgentDeps):
        """
        Saves the created branch to the database.
        """
        session: Session = deps.session
        # Try to find the repository by URL
        # repository_url might be slightly different (with or without .git, etc.)
        # For now, let's do a simple check or try to match it.
        repo = session.query(ProjectRepository).filter(
            (ProjectRepository.url == code_git_output.repository_url) &
            (ProjectRepository.project_id == deps.project_id)
        ).first()

        if not repo:
            logger.warning(f"Repository with URL {code_git_output.repository_url} not found in project {deps.project_id}")
            # Optionally try to match without .git if present
            if code_git_output.repository_url.endswith(".git"):
                url_without_git = code_git_output.repository_url[:-4]
                repo = session.query(ProjectRepository).filter(
                    (ProjectRepository.url == url_without_git) &
                    (ProjectRepository.project_id == deps.project_id)
                ).first()
            elif not code_git_output.repository_url.endswith(".git"):
                url_with_git = code_git_output.repository_url + ".git"
                repo = session.query(ProjectRepository).filter(
                    (ProjectRepository.url == url_with_git) &
                    (ProjectRepository.project_id == deps.project_id)
                ).first()

        if repo and code_git_output.branch_name:
            # Check if we have a user_story_id in deps
            user_story_id = None
            if deps.input_type == "user_story":
                user_story_id = deps.input_id

            if user_story_id:
                branch_create = UserStoryBranchCreate(
                    user_story_id=user_story_id,
                    branch_url=code_git_output.branch_url,
                    branch_name=code_git_output.branch_name,
                    pull_request_url=code_git_output.pull_request_url,
                    project_repository_id=repo.id
                )
                user_story_branch_service.create_branch(session, branch_create)
                logger.info(f"Saved branch {code_git_output.branch_name} for user story {user_story_id}")
            else:
                logger.warning("No user_story_id found in deps, cannot save branch info")
        else:
            logger.warning(f"Could not find project repository for URL: {code_git_output.repository_url}")


user_story_branch_service = UserStoryBranchService()
