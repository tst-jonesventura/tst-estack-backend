from database.models import (
    Agent,
    AgentContext,
    AgentLog,
    AgentMcp,
    AgentRun,
    AgentTool,
    Context,
    DocumentTranscription,
    GherkinRule,
    GherkinScenario,
    Mcp,
    Project,
    ProjectAgent,
    ProjectRepository,
    Role,
    Tag,
    TechSpecTag,
    Tool,
    TranscriptionTag,
    User,
    UserAccessCode,
    UserStory,
    UserStoryDevStep,
    UserStoryTag,
)


class TestFixturesSmoke:
    def test_sample_admin_user(self, sample_admin_user: User):
        assert sample_admin_user.is_admin is True
        assert sample_admin_user.email == "admin@example.com"

    def test_sample_inactive_user(self, sample_inactive_user: User):
        assert sample_inactive_user.is_active is False

    def test_sample_contributor_role(self, sample_contributor_role: Role):
        assert sample_contributor_role.name == "contributor"

    def test_sample_viewer_role(self, sample_viewer_role: Role):
        assert sample_viewer_role.name == "viewer"

    def test_sample_second_project(self, sample_second_project: Project):
        assert sample_second_project.name == "Second Project"

    def test_sample_second_transcription(
        self, sample_second_transcription: DocumentTranscription
    ):
        assert sample_second_transcription.title == "Second Transcription"

    def test_sample_second_user_story(self, sample_second_user_story: UserStory):
        assert sample_second_user_story.name == "Second User Story"
        assert sample_second_user_story.is_done is True

    def test_sample_user_story_dev_step(
        self, sample_user_story_dev_step: UserStoryDevStep
    ):
        assert sample_user_story_dev_step.order == 1
        assert sample_user_story_dev_step.name == "Setup environment"

    def test_sample_second_dev_step(self, sample_second_dev_step: UserStoryDevStep):
        assert sample_second_dev_step.order == 2

    def test_sample_project_repository(
        self, sample_project_repository: ProjectRepository
    ):
        assert "github.com" in sample_project_repository.url

    def test_sample_second_tag(self, sample_second_tag: Tag):
        assert sample_second_tag.tag == "integration"

    def test_sample_user_story_tag(self, sample_user_story_tag: UserStoryTag):
        assert sample_user_story_tag.user_story_id is not None
        assert sample_user_story_tag.tag_id is not None

    def test_sample_tech_spec_tag(self, sample_tech_spec_tag: TechSpecTag):
        assert sample_tech_spec_tag.tech_spec_id is not None
        assert sample_tech_spec_tag.tag_id is not None

    def test_sample_transcription_tag(
        self, sample_transcription_tag: TranscriptionTag
    ):
        assert sample_transcription_tag.transcription_id is not None
        assert sample_transcription_tag.tag_id is not None

    def test_sample_tool(self, sample_tool: Tool):
        assert sample_tool.name == "get_full_project_info"

    def test_sample_second_tool(self, sample_second_tool: Tool):
        assert sample_second_tool.name == "get_stories_by_input_tags"

    def test_sample_context(self, sample_context: Context):
        assert sample_context.name == "get_full_project_info"

    def test_sample_mcp(self, sample_mcp: Mcp):
        assert sample_mcp.name == "test-mcp-server"

    def test_sample_agent(self, sample_agent: Agent):
        assert sample_agent.name == "transcription_to_user_story"
        assert sample_agent.is_default is True

    def test_sample_second_agent(self, sample_second_agent: Agent):
        assert sample_second_agent.name == "labeler_agent"
        assert sample_second_agent.is_default is False

    def test_sample_agent_tool(self, sample_agent_tool: AgentTool):
        assert sample_agent_tool.agent_id is not None
        assert sample_agent_tool.tool_name == "get_full_project_info"

    def test_sample_agent_context(self, sample_agent_context: AgentContext):
        assert sample_agent_context.agent_id is not None
        assert sample_agent_context.context_name == "get_full_project_info"

    def test_sample_agent_mcp(self, sample_agent_mcp: AgentMcp):
        assert sample_agent_mcp.agent_id is not None
        assert sample_agent_mcp.mcp_name == "test-mcp-server"

    def test_sample_project_agent(self, sample_project_agent: ProjectAgent):
        assert sample_project_agent.project_id is not None
        assert sample_project_agent.agent_id is not None

    def test_sample_agent_run(self, sample_agent_run: AgentRun):
        assert sample_agent_run.status == "completed"
        assert sample_agent_run.title == "Test Agent Run"

    def test_sample_agent_log(self, sample_agent_log: AgentLog):
        assert "started" in sample_agent_log.message

    def test_sample_second_agent_log(self, sample_second_agent_log: AgentLog):
        assert "finished" in sample_second_agent_log.message

    def test_sample_user_access_code(self, sample_user_access_code: UserAccessCode):
        assert sample_user_access_code.access_code == "1234"

    def test_sample_second_gherkin_rule(
        self, sample_second_gherkin_rule: GherkinRule
    ):
        assert sample_second_gherkin_rule.index == 1

    def test_sample_second_gherkin_scenario(
        self, sample_second_gherkin_scenario: GherkinScenario
    ):
        assert sample_second_gherkin_scenario.index == 1
        assert sample_second_gherkin_scenario.is_done is True
