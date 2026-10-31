"""A self-contained mock Redmine REST API for demos and tests.

Run it with ``python -m redmine_agent mock-server`` and point ``REDMINE_URL`` at it
(API key: ``demo-api-key``). Data is generated deterministically relative to "now".
"""

from .server import MockRedmine, start_mock_server  # noqa: F401
