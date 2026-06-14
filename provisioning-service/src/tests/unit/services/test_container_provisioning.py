"""Unit tests for the container provisioning path (WS-A).

Covers: _build_container_vars (YAML), build_vars_file routing on
provisioning_type, _extract_ansible_json container fact names, and the
CreateContainerRequest / build_simple_container_params model layer.
Mirrors test_ansible_service.py conventions.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from models.container_request_model import (
    ContainerActionRequest,
    CreateContainerRequest,
    build_simple_container_params,
)
from models.jobs_model import AnsibleJobParams
from services.ansible_service import AnsibleService


def _make_service() -> AnsibleService:
    settings = MagicMock()
    settings.resolved_playbook_path = "/playbooks/vm-operations.yaml"
    settings.resolved_container_playbook_path = "/playbooks/container-operations.yaml"
    settings.resolved_inventory_path = "/inventory/hosts"
    settings.ansible_timeout_seconds = 1800
    return AnsibleService(settings)


def _container_params(**overrides) -> AnsibleJobParams:
    defaults = dict(
        vm_host="aex-native-scm",
        vm_target="aex-t-1",
        vm_action="create",
        provisioning_type="container",
    )
    defaults.update(overrides)
    return AnsibleJobParams(**defaults)


def _lines(yaml_str: str) -> dict[str, str]:
    result = {}
    for line in yaml_str.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


class TestBuildContainerVars:
    def test_required_fields(self):
        out = _lines(_make_service()._build_container_vars(_container_params()))
        assert out["container_host"] == "aex-native-scm"
        assert out["container_action"] == "create"
        assert out["container_target"] == "aex-t-1"

    def test_image_env_lease(self):
        p = _container_params(
            container_image="img:1", container_env={"AGENT_NAME": "a1"}, lease_id="0xabc"
        )
        out = _make_service()._build_container_vars(p)
        assert 'container_image: "img:1"' in out
        assert '"AGENT_NAME": "a1"' in out  # json.dumps of the env dict
        assert 'lease_id: "0xabc"' in out

    def test_build_vars_file_routes_to_container(self):
        path = _make_service().build_vars_file(_container_params())
        try:
            assert path.name.startswith("container_vars_")
            assert "container_action: create" in path.read_text()
        finally:
            path.unlink(missing_ok=True)

    def test_build_vars_file_vm_path_unchanged(self):
        path = _make_service().build_vars_file(
            AnsibleJobParams(vm_host="kvm1", vm_target="vm1", vm_action="create")
        )
        try:
            assert path.name.startswith("vm_vars_")
            assert "vm_host: kvm1" in path.read_text()
        finally:
            path.unlink(missing_ok=True)


class TestExtractContainerJson:
    def test_extracts_container_creation_data(self):
        svc = _make_service()
        stdout = (
            'TASK [debug]\n"container_creation_data": {\n'
            '  "container_name": "aex-t-1",\n  "running": true\n}\n'
        )
        result = svc._extract_ansible_json(stdout, "create", provisioning_type="container")
        assert result is not None
        assert result["container_name"] == "aex-t-1"
        assert result["running"] is True

    def test_archive_restore_fact_names(self):
        svc = _make_service()
        for action, fact in [
            ("archive", "container_archive_data"),
            ("restore", "container_restore_data"),
        ]:
            stdout = f'"{fact}": {{"container_name": "x", "ok": true}}'
            r = svc._extract_ansible_json(stdout, action, provisioning_type="container")
            assert r and r["container_name"] == "x"

    def test_vm_extraction_unaffected(self):
        r = _make_service()._extract_ansible_json(
            '"vm_creation_data": {"vm_name": "v1"}', "create"
        )
        assert r and r["vm_name"] == "v1"


class TestContainerRequestModel:
    def test_create_to_params(self):
        req = CreateContainerRequest(
            container_target="aex-t-1",
            container_image="img:1",
            container_env={"K": "V"},
            lease_id="0xabc",
        )
        p = req.to_ansible_job_params("aex-native-scm")
        assert p.provisioning_type == "container"
        assert (p.vm_action, p.vm_target, p.vm_host) == ("create", "aex-t-1", "aex-native-scm")
        assert p.container_image == "img:1"
        assert p.container_env == {"K": "V"}
        assert p.lease_id == "0xabc"

    def test_simple_params(self):
        p = build_simple_container_params(
            "destroy", "aex-native-scm", ContainerActionRequest(), "aex-t-1"
        )
        assert p.provisioning_type == "container"
        assert (p.vm_action, p.vm_target) == ("destroy", "aex-t-1")
