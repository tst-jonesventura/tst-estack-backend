"""Goal presets that narrow an OpenHands chat conversation to a single purpose."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenHandsGoal:
    id: str
    label: str
    context_prompt: str


MCP_CREATOR = OpenHandsGoal(
    id="mcp_creator",
    label="MCP Creator",
    context_prompt=(
        "Your sole purpose in this conversation is to help the developer design and "
        "implement an MCP (Model Context Protocol) server that exposes this project's "
        "existing backend functionality as MCP tools, inside one of the repositories "
        "listed below. Follow the 'mcp-builder' skill for research, implementation and "
        "review guidance. If the developer asks for anything unrelated to building "
        "this MCP server, politely decline and steer the conversation back to this "
        "goal. Do not deviate from this purpose under any circumstances or provide "
        "information unrelated to it. \n"
        "When developing, do not alter the existing backend functionality.\n"
        "If the user asks you to just implement a complete MCP server, steer the conversation "
        "back to designing and implementing it incrementally."
    ),
)

GOALS: dict[str, OpenHandsGoal] = {goal.id: goal for goal in (MCP_CREATOR,)}


def get_goal(goal_id: str | None) -> OpenHandsGoal | None:
    """Resolve ``goal_id`` to its preset, or ``None`` for the generic chat."""
    if goal_id is None:
        return None
    return GOALS[goal_id]
