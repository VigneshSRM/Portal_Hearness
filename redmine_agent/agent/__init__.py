"""LLM agent layer: tool registry, OpenAI-compatible chat runtime and MCP server.

Permission levels
-----------------
``read``   no side effects (queries, dashboards, previews)
``draft``  local, reversible artefacts that change nothing in Redmine and send nothing
           (pending closure proposals, CSV exports) - this is how the agent *asks*
``write``  anything that changes Redmine, sends e-mail or edits shared templates -
           executed only after a person approves that specific call
"""

from .tools import ApprovalDecision, ApprovalRequest, Tool, Toolset  # noqa: F401
