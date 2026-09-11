"""Compatibility import for the application-layer workflow orchestrator."""

try:
    from ..application.workflow_service import WorkflowService
except ImportError:  # Legacy direct-script import path.
    from application.workflow_service import WorkflowService

__all__ = ["WorkflowService"]
