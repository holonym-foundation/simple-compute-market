"""Container lifecycle controller (AEX-native container tenants).

All container operations are scoped to a host identified in the URL:

    /api/v1/hosts/{host}/containers/...

``host`` is the Ansible inventory alias for a ``[container_hosts]`` Docker host
(e.g. ``aex-native-scm``). ``container_name`` is the container name. Mirrors
``VmController``: every mutating endpoint submits an Ansible job (routed to the
container-operations playbook via ``provisioning_type="container"``) and returns
a ``JobSubmitResponse``; callers poll ``GET /api/v1/jobs/{job_id}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi_utils.cbv import cbv

import container as _container_module
from models.container_request_model import (
    ContainerActionRequest,
    CreateContainerRequest,
    build_simple_container_params,
)
from models.jobs_model import JobSubmitResponse
from services.job_service import AnsibleJobService

router = APIRouter(prefix="/hosts/{host}/containers", tags=["containers"])

_POLL_NOTE = (
    "Poll ``GET /api/v1/jobs/{job_id}`` for status. "
    "Terminal statuses: ``succeeded``, ``failed``, ``cancelled``."
)


@cbv(router)
class ContainerController:
    def __init__(
        self,
        job_service: AnsibleJobService = Depends(
            lambda: _container_module.resolved_job_service
        ),
    ) -> None:
        self._job_service = job_service

    async def _submit(self, params) -> JobSubmitResponse:
        return await self._job_service.submit(
            params, _container_module.resolved_job_queue
        )

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    @router.post(
        "/",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Create a container",
    )
    async def create_container(
        self, host: str, body: CreateContainerRequest
    ) -> JobSubmitResponse:
        """Provision a new container tenant on ``host``. """ + _POLL_NOTE
        return await self._submit(body.to_ansible_job_params(host))

    @router.get(
        "/",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="List AEX-managed containers on a host",
    )
    async def list_containers(
        self, host: str, body: ContainerActionRequest = Depends()
    ) -> JobSubmitResponse:
        """List AEX-managed containers on ``host``. """ + _POLL_NOTE
        return await self._submit(build_simple_container_params("list", host, body))

    # ------------------------------------------------------------------
    # Instance lifecycle
    # ------------------------------------------------------------------

    @router.post(
        "/{container_name}/start",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Start a stopped container",
    )
    async def start_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Start a stopped container. """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("start", host, body, container_name)
        )

    @router.post(
        "/{container_name}/stop",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Stop a running container (volume retained)",
    )
    async def stop_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Stop a running container; the home volume is retained. """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("stop", host, body, container_name)
        )

    @router.post(
        "/{container_name}/destroy",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Destroy a container and its home volume",
    )
    async def destroy_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Remove the container and its home volume (credential gone). """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("destroy", host, body, container_name)
        )

    @router.get(
        "/{container_name}/monitor",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Monitor a container",
    )
    async def monitor_container(
        self, host: str, container_name: str, body: ContainerActionRequest = Depends()
    ) -> JobSubmitResponse:
        """Report container status/health. """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("monitor", host, body, container_name)
        )

    @router.post(
        "/{container_name}/lease-end",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="End the lease (stop, keep volume)",
    )
    async def lease_end_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Stop at lease end but KEEP the home volume (resume/reclaim). """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("lease_end", host, body, container_name)
        )

    @router.post(
        "/{container_name}/archive",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Archive container state to cold storage",
    )
    async def archive_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Export the home volume to object storage, then free the node. """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("archive", host, body, container_name)
        )

    @router.post(
        "/{container_name}/restore",
        response_model=JobSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Restore an archived container's state",
    )
    async def restore_container(
        self, host: str, container_name: str, body: ContainerActionRequest
    ) -> JobSubmitResponse:
        """Pull the archive and recreate the home volume (then create). """ + _POLL_NOTE
        return await self._submit(
            build_simple_container_params("restore", host, body, container_name)
        )

    @classmethod
    def make_router(cls) -> APIRouter:
        return router
