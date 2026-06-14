"""Typed request models for container (AEX-native) tenant operations.

Mirrors ``vm_request_model.py`` but for Docker container tenants on plain hosts
(no nested-virt). Each model maps to one container action and produces an
``AnsibleJobParams`` with ``provisioning_type="container"`` — which routes the
job to the container-operations playbook and the container_* var/result shapes.

``host`` (container host alias) and ``container_name`` come from the URL path,
not the body — same convention as the VM models.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.jobs_model import AnsibleJobParams


class ContainerActionRequest(BaseModel):
    """Optional body fields shared by all single-target container operations."""

    max_retries: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description="Per-job retry limit override (default from service config)",
    )


class CreateContainerRequest(BaseModel):
    """Provision a new container tenant on the host identified in the URL.

    ``POST /api/v1/hosts/{host}/containers``

    Returns a ``JobSubmitResponse`` with a ``job_id``; poll
    ``GET /api/v1/jobs/{job_id}`` for status. On success ``result`` carries the
    container id/state from ``container_creation_data``.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "container_target": "aex-t-airdrop-scout-01",
                    "container_image": "ghcr.io/holonym-foundation/aex-agent-base:latest",
                    "container_env": {"AGENT_NAME": "airdrop-scout-01"},
                    "lease_id": "0xabc...",
                }
            ]
        }
    }

    container_target: str = Field(description="Container name (unique per host)")
    container_image: Optional[str] = Field(
        default=None,
        description="Image ref to run; defaults to the role's container_default_image",
    )
    container_env: Optional[dict] = Field(
        default=None,
        description="Environment for the agent (recipe config + telemetry DSN, etc.)",
    )
    lease_id: Optional[str] = Field(
        default=None,
        description="On-chain lease/escrow id; labels the container for reconciliation",
    )
    max_retries: Optional[int] = Field(default=None, ge=0, le=10)

    def to_ansible_job_params(self, host: str) -> AnsibleJobParams:
        """Build ``AnsibleJobParams`` using path-supplied ``host``."""
        return AnsibleJobParams(
            vm_host=host,
            vm_action="create",
            vm_target=self.container_target,
            provisioning_type="container",
            container_image=self.container_image,
            container_env=self.container_env,
            lease_id=self.lease_id,
            max_retries=self.max_retries,
        )


def build_simple_container_params(
    action: str,
    host: str,
    body: ContainerActionRequest,
    container_name: Optional[str] = None,
) -> AnsibleJobParams:
    """Produce ``AnsibleJobParams`` for container actions whose only inputs are
    the path parameters (start/stop/destroy/monitor/lease_end/archive/restore/
    list/check). ``container_name`` is ``None`` for host-level actions (list)."""
    return AnsibleJobParams(
        vm_host=host,
        vm_action=action,
        vm_target=container_name,
        provisioning_type="container",
        max_retries=body.max_retries,
    )
