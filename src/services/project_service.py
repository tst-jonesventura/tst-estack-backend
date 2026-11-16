from database.database import SessionDep
from database.models import Project, UserProject, Role
from utils import log

logger = log.setup_custom_logger(__name__)


class ProjectService:
    def __init__(self):
        # Stateless service - no initialization required.
        # All methods receive session and required parameters directly.
        pass

    def projects_owned_by_user(self, session: SessionDep, user_id):
        projects_owned_by_user = (
            session.query(Project)
            .where(Project.is_active)
            .where(
                Project.user_roles.any(
                    (UserProject.user_id == user_id)
                    & UserProject.role.has(Role.name == "owner")
                )
            )
            .order_by(Project.name.asc())
            .all()
        )

        if not projects_owned_by_user:
            return []

        return projects_owned_by_user


project_service = ProjectService()
