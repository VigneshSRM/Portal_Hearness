"""Composition root: builds every service from :class:`Settings`."""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import ClosureWorkflow
from .client import RedmineClient
from .closure_templates import TemplateStore
from .config import Settings, load_settings
from .data import DataService
from .guard import WriteGuard
from .store import HarnessStore


@dataclass
class Harness:
    settings: Settings
    client: RedmineClient
    store: HarnessStore
    data: DataService
    templates: TemplateStore
    workflow: ClosureWorkflow

    @classmethod
    def create(cls, settings: Settings | None = None, *, client: RedmineClient | None = None) -> "Harness":
        settings = settings or load_settings()
        store = HarnessStore(settings.data_path / "harness.db")
        guard = WriteGuard(read_only=settings.redmine.read_only, audit=store.audit)
        if client is None:
            client = RedmineClient.from_settings(settings, guard=guard)
        else:
            client.guard = guard
        data = DataService(client, settings, store)
        seed = settings.closure.seed_templates_file
        templates = TemplateStore(
            settings.path(settings.closure.templates_file),
            settings.path(seed) if seed else None,
        )
        workflow = ClosureWorkflow(client, data, store, templates, settings)
        # Every Redmine write must be backed by a human-approved proposal in the store.
        guard.verifier = workflow.verify_approval
        return cls(settings=settings, client=client, store=store, data=data, templates=templates,
                   workflow=workflow)

    @property
    def read_only(self) -> bool:
        return self.client.guard.read_only
