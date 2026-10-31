"""Permission guard: the technical enforcement of "no changes without permission".

Every request that could change Redmine (POST/PUT/PATCH/DELETE) must pass
through :meth:`WriteGuard.authorize`. Authorization succeeds only when:

1. the harness is not in read-only mode, and
2. an :class:`Approval` is supplied whose action and target match the request, and
3. the approval's payload digest equals the SHA-256 of the *exact* payload being
   sent (so a reviewed change cannot be silently altered afterwards), and
4. the optional ``verifier`` (wired to the approval store) confirms that a real
   human approved that proposal and it is still in the ``approved`` state.

The low-level HTTP layer in :mod:`redmine_agent.client` refuses any non-GET
request that did not go through this guard, so there is no "back door".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from .errors import PermissionDenied

#: Actor names that are never accepted as a human approver.
NON_HUMAN_ACTORS = frozenset({"", "agent", "llm", "assistant", "automation", "scheduler", "system", "mcp"})


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_digest(payload: Any) -> str:
    """Stable SHA-256 digest of a JSON-serialisable payload."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def is_human_actor(name: str | None) -> bool:
    if not name:
        return False
    normalized = name.strip().lower()
    if normalized in NON_HUMAN_ACTORS:
        return False
    return not normalized.startswith(("agent:", "automation:", "scheduler:", "system:", "mcp:"))


@dataclass(frozen=True)
class Approval:
    """Proof that a human approved one specific change."""

    proposal_id: str
    action: str  # e.g. "update_issue"
    target: str  # e.g. "issue:123"
    payload_digest: str
    approved_by: str
    approved_at: str


class WriteGuard:
    def __init__(
        self,
        *,
        read_only: bool = False,
        verifier: Callable[[Approval], None] | None = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.read_only = read_only
        self.verifier = verifier
        self.audit = audit

    def authorize(self, action: str, target: str, payload: Any, approval: Approval | None) -> None:
        try:
            self._check(action, target, payload, approval)
        except PermissionDenied as exc:
            if self.audit:
                self.audit(actor=approval.approved_by if approval else "unknown",
                           action=f"guard.denied:{action}", target=target,
                           outcome="denied", details={"reason": str(exc)})
            raise

    def _check(self, action: str, target: str, payload: Any, approval: Approval | None) -> None:
        if self.read_only:
            raise PermissionDenied(
                "The harness is in READ-ONLY mode (redmine.read_only / REDMINE_READ_ONLY=true). "
                "No changes can be made to Redmine."
            )
        if approval is None:
            raise PermissionDenied(
                f"'{action}' on {target} requires explicit human approval. "
                "Create a proposal and have a person approve it first."
            )
        if approval.action != action or approval.target != target:
            raise PermissionDenied(
                f"The approval is for '{approval.action}' on {approval.target}, "
                f"not '{action}' on {target}."
            )
        if not is_human_actor(approval.approved_by):
            raise PermissionDenied(
                f"'{approval.approved_by or 'unknown'}' is not a human reviewer; "
                "automated actors cannot approve changes."
            )
        if approval.payload_digest != payload_digest(payload):
            raise PermissionDenied(
                "The change being sent differs from what was approved. Re-review is required."
            )
        if self.verifier is not None:
            self.verifier(approval)
