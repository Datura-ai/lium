"""Lium SDK - Clean, Unix-style SDK for GPU pod management."""

import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import paramiko
import requests
from dotenv import load_dotenv

from .config import Config
from .exceptions import (
    LiumAuthError,
    LiumError,
    LiumNotFoundError,
    LiumRateLimitError,
    LiumServerError,
)
from .models import (
    BackupConfig,
    BackupLog,
    ExecutorInfo,
    PodInfo,
    Template,
    VolumeInfo,
)
from .utils import expand_gpu_shorthand, extract_gpu_type, generate_huid, with_retry

load_dotenv()

# Main SDK Class
class Lium:
    """Clean Unix-style SDK for Lium."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config.load()
        self.headers = {"X-API-KEY": self.config.api_key}

    @with_retry()
    def _request(
        self,
        method: str,
        endpoint: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> requests.Response:
        """Make API request with error handling."""
        url = f"{base_url or self.config.base_url}/{endpoint.lstrip('/')}"
        request_headers = headers or self.headers
        resp = requests.request(method, url, headers=request_headers, timeout=30, **kwargs)

        if resp.ok:
            return resp

        # Map errors
        if resp.status_code == 401:
            raise LiumAuthError("Invalid API key")
        if resp.status_code == 404:
            raise LiumNotFoundError(f"Resource not found: {resp.text}")
        if resp.status_code == 429:
            raise LiumRateLimitError("Rate limit exceeded")
        if 500 <= resp.status_code < 600:
            raise LiumServerError(f"Server error: {resp.status_code}")
        raise LiumError(f"API error {resp.status_code}: {resp.text}")

    def _dict_to_backup_config(self, config_dict: Dict) -> BackupConfig:
        """Convert backup config dict to BackupConfig object."""
        return BackupConfig(
            id=config_dict.get("id", ""),
            huid=generate_huid(config_dict.get("id", "")),
            pod_executor_id=config_dict.get("pod_executor_id", ""),
            backup_frequency_hours=config_dict.get("backup_frequency_hours", 0),
            retention_days=config_dict.get("retention_days", 0),
            backup_path=config_dict.get("backup_path", ""),
            is_active=config_dict.get("is_active", True),
            created_at=config_dict.get("created_at", ""),
            updated_at=config_dict.get("updated_at")
        )

    def _dict_to_backup_log(self, log_dict: Dict) -> BackupLog:
        """Convert backup log dict to BackupLog object."""
        return BackupLog(
            id=log_dict.get("id", ""),
            huid=generate_huid(log_dict.get("id", "")),
            backup_config_id=log_dict.get("backup_config_id", ""),
            status=log_dict.get("status", "unknown"),
            started_at=log_dict.get("started_at", ""),
            completed_at=log_dict.get("completed_at"),
            error_message=log_dict.get("error_message"),
            progress=log_dict.get("progress"),
            backup_volume_id=log_dict.get("backup_volume_id"),
            created_at=log_dict.get("created_at")
        )

    def _dict_to_volume_info(self, volume_dict: Dict) -> VolumeInfo:
        """Convert volume dict to VolumeInfo object."""
        return VolumeInfo(
            id=volume_dict.get("id", ""),
            huid=generate_huid(volume_dict.get("id", "")),
            name=volume_dict.get("name", ""),
            description=volume_dict.get("description", ""),
            created_at=volume_dict.get("created_at", ""),
            updated_at=volume_dict.get("updated_at"),
            current_size_bytes=volume_dict.get("current_size_bytes", 0),
            current_file_count=volume_dict.get("current_file_count", 0),
            current_size_gb=volume_dict.get("current_size_gb", 0.0),
            current_size_mb=volume_dict.get("current_size_mb", 0.0),
            last_metrics_update=volume_dict.get("last_metrics_update")
        )

    def _dict_to_executor_info(self, executor_dict: Dict) -> Optional[ExecutorInfo]:
        """Convert executor dict to ExecutorInfo object."""
        if not executor_dict:
            return None

        # Extract GPU info from specs or machine_name
        specs = executor_dict.get("specs", {})
        gpu_info = specs.get("gpu", {})
        gpu_count = gpu_info.get("count", 1)
        available_gpu_count = executor_dict.get("available_gpu_count", 1)

        # Extract GPU type from machine_name or specs
        machine_name = executor_dict.get("machine_name", "")
        gpu_type = extract_gpu_type(machine_name)

        # If we couldn't extract from machine_name, try specs
        if gpu_type == machine_name.split()[-1] and gpu_info.get("details"):
            gpu_details = gpu_info.get("details", [])
            if gpu_details:
                gpu_name = gpu_details[0].get("name", "")
                if gpu_name:
                    gpu_type = extract_gpu_type(gpu_name)

        price_per_hour = executor_dict.get("price_per_hour", 0)
        price_per_gpu = executor_dict.get("price_per_gpu", 0)

        return ExecutorInfo(
            id=executor_dict.get("id", ""),
            ip=executor_dict.get("executor_ip_address", ""),
            huid=generate_huid(executor_dict.get("id", "")),
            machine_name=machine_name,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            available_gpu_count=available_gpu_count,
            price_per_hour=price_per_hour,
            price_per_gpu_hour=price_per_gpu,
            location=executor_dict.get("location", {}),
            specs=specs,
            status=executor_dict.get("status", "unknown"),
            docker_in_docker=specs.get("sysbox_runtime", False),
            available_port_count=specs.get("available_port_count"),
        )

    def up(
        self,
        *,
        executor_id: str,
        name: Optional[str] = None,
        gpu_count: Optional[int] = None,
        template_id: Optional[str] = None,
        volume_id: Optional[str] = None,
        ports: Optional[int] = None,
        ssh_keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Start a new pod on a specific executor.

        Args:
            executor_id: Target executor ID string.
            name: Human-friendly pod name (defaults to ``"Your Pod"``).
            template_id: Template ID. Defaults to the executor's default template.
            volume_id: Optional volume ID to attach on spawn.
            ports: Number of exposed ports to request.
            ssh_keys: SSH public keys to authorize. Defaults to the keys discovered by the Config.

        Returns:
            Pod metadata as returned by the rent API (id, name, status, ssh command, etc.).
        """
        executor_info = self.get_executor(executor_id)
        if not executor_info:
            raise ValueError(f"Executor with ID '{executor_id}' not found")

        if template_id is None:
            selected_template = self.default_docker_template(executor_info.id)
            template_id = selected_template.id

        ssh_material = ssh_keys or self.config.ssh_public_keys
        if not ssh_material:
            raise ValueError("No SSH keys found")

        payload = {
            "pod_name": name,
            "gpu_count": gpu_count,
            "template_id": template_id,
            "volume_id": volume_id,
            "user_public_key": ssh_material,
            "initial_port_count": ports,
        }

        response = self._request("POST", f"/executors/{executor_info.id}/rent", json=payload).json()

        # API should return pod info
        if response and "id" in response:
            return response

        # Fallback: find pod by name after creation
        if name:
            for _ in range(2):
                time.sleep(3)
                for pod in self.ps():
                    if pod.name == name:
                        return {
                            "id": pod.id,
                            "name": pod.name,
                            "status": pod.status,
                            "huid": pod.huid,
                            "ssh_cmd": pod.ssh_cmd,
                            "executor_id": executor_info.id
                        }

        raise LiumError(f"Failed to create pod{' ' + name if name else ''}")

    def pod(
        self,
        pod_id: str
    ) -> Dict[str, Any]:
        """Retrieve detailed information about a specific pod.

        Args:
            pod_id: The unique identifier of the pod to retrieve.

        Returns:
            Raw pod data dictionary including template, executor, status, and connection info.
        """
        return self._request("GET", f"/pods/{pod_id}").json()

    def logs(
        self,
        pod_id: str,
        *,
        tail: int = 100,
        follow: bool = False,
    ) -> Generator[bytes, None, None]:
        """Stream logs from a pod.

        Args:
            pod_id: The unique identifier of the pod.
            tail: Number of lines to retrieve from the end of the logs (default: 100).
            follow: If True, stream logs continuously (default: False).

        Yields:
            Log lines as bytes.
        """
        params = {"tail": tail, "follow": str(follow).lower()}
        url = f"{self.config.base_url}/pods/{pod_id}/logs"

        with requests.get(url, headers=self.headers, params=params, stream=True, timeout=None if follow else 30) as response:
            if not response.ok:
                if response.status_code == 401:
                    raise LiumAuthError("Invalid API key")
                if response.status_code == 404:
                    raise LiumNotFoundError(f"Pod not found: {pod_id}")
                if response.status_code == 429:
                    raise LiumRateLimitError("Rate limit exceeded")
                if 500 <= response.status_code < 600:
                    raise LiumServerError(f"Server error: {response.status_code}")
                raise LiumError(f"API error {response.status_code}: {response.text}")

            for line in response.iter_lines():
                if line:
                    yield line

    def edit(
        self,
        pod_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Edit a pod's template configuration.

        Updates the template associated with a pod by merging the provided
        keyword arguments with the existing template settings.

        Args:
            pod_id: The unique identifier of the pod whose template to edit.
            **kwargs: Template fields to update. Common fields include:
                - docker_image (str): Docker image repository.
                - docker_image_tag (str): Docker image tag.
                - startup_commands (str): Commands to run on container start.
                - internal_ports (List[int]): Ports to expose.
                - environment (Dict[str, str]): Environment variables.
                - volumes (List[str]): Volume mount paths.

        Returns:
            Updated template data dictionary from the API.

        Example:
            >>> lium.edit(pod_id, startup_commands="python main.py", environment={"DEBUG": "1"})
        """
        pod = self.pod(pod_id=pod_id)

        payload = {
            **pod["template"],
            **kwargs,
        }

        return self._request("PUT", f"/templates/{pod['template']['id']}", json=payload).json()

    def ls(
        self,
        *,
        gpu_type: Optional[str] = None,
        gpu_count: Optional[int] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        max_distance_miles: Optional[int] = None,
    ) -> List[ExecutorInfo]:
        """List available executors.

        Args:
            gpu_type: Optional GPU filter such as ``"A100"`` or ``"H200"``.
            gpu_count: Exact GPU count to match (defaults to 8, pass ``None`` to disable).
            lat: Optional latitude for geospatial filtering. Must be used together with ``lon`` and ``max_distance_miles``.
            lon: Optional longitude for geospatial filtering. Must be used together with ``lat`` and ``max_distance_miles``.
            max_distance_miles: Optional radius (in miles) for geospatial filtering. Must be used together with ``lat`` and ``lon``.

        Returns:
            A list of :class:`ExecutorInfo` objects that satisfy the filters.
        """
        params: Dict[str, Any] = {"size": 1000}
        if gpu_type:
            # Try to map short GPU name to full machine name
            machine_name = self._resolve_machine_name(gpu_type)
            if machine_name:
                params["machine_names"] = machine_name
            else:
                # If no match found, use the input as-is (might be already a full name)
                params["machine_names"] = gpu_type
        if gpu_count:
            params["gpu_count_gte"] = gpu_count
            # params["gpu_count_lte"] = gpu_count
        if lat is not None and lon is not None:
            params["lat"] = lat
            params["lon"] = lon
            if max_distance_miles is not None:
                params["max_distance_mile"] = max_distance_miles
        elif max_distance_miles is not None:
            params["max_distance_mile"] = max_distance_miles

        data = self._request("GET", "/executors", params=params).json()
        executors = [self._dict_to_executor_info(d) for d in data]
        executors = [e for e in executors if e]  # Filter None values

        return executors

    def ps(self) -> List[PodInfo]:
        """List active pods.

        Returns:
            List of :class:`PodInfo` objects representing the caller's running pods.
        """
        data = self._request("GET", "/pods").json()

        pods = [
            PodInfo(
                id=d.get("id", ""),
                name=d.get("pod_name", ""),
                status=d.get("status", "unknown"),
                huid=generate_huid(d.get("id", "")),
                gpu_count=int(d.get("gpu_count", 0)),
                price=d.get("price", 0.0),
                ssh_cmd=d.get("ssh_connect_cmd"),
                ports=d.get("ports_mapping", {}),
                created_at=d.get("created_at", ""),
                updated_at=d.get("updated_at", ""),
                executor=self._dict_to_executor_info(d.get("executor", {})) if d.get("executor") else None,
                template=d.get("template", {}),
                removal_scheduled_at=d.get("removal_scheduled_at"),
                jupyter_installation_status=d.get("jupyter_installation_status"),
                jupyter_url=d.get("jupyter_url")
            )
            for d in data
        ]

        return pods

    def down(self, pod: PodInfo) -> Dict[str, Any]:
        """Stop a pod.

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        return self._request("DELETE", f"/pods/{pod.id}").json()

    def rm(self, pod: PodInfo) -> Dict[str, Any]:
        """Remove pod (alias for :meth:`down`).

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        return self.down(pod)

    def reboot(self, pod: PodInfo, volume_id: Optional[str] = None) -> Dict[str, Any]:
        """Reboot a pod.

        Args:
            pod: Pod to reboot.
            volume_id: Optional volume ID to attach for the reboot request.

        Returns:
            Pod data from the API response after issuing the reboot.
        """
        payload: Dict[str, Optional[str]] = {}
        if volume_id is not None:
            payload["volume_id"] = volume_id

        return self._request("POST", f"/pods/{pod.id}/reboot", json=payload or {}).json()

    def get_default_images(self, gpu_model: Optional[str], driver_version: Optional[str]) -> list[dict]:
        """Get default images for GPU type and driver version."""
        params = {
            "gpu_model": gpu_model,
            "driver_version": driver_version
        }
        data = self._request("GET", "/executors/default-docker-image", params=params).json()
        return data

    def _select_fallback_template(self) -> Optional[Template]:
        """Pick a reasonable default template (prefer PyTorch, else first available)."""
        templates = self.templates()
        if not templates:
            return None

        def is_pytorch(template: Template) -> bool:
            category = (template.category or "").upper()
            image = (template.docker_image or "").lower()
            return "PYTORCH" in category or "pytorch" in image

        pytorch_templates = [t for t in templates if is_pytorch(t)]

        if pytorch_templates:
            def version_key(template: Template):
                tag = template.docker_image_tag or ""
                version_part = tag.split('-')[0]
                parts = []
                for piece in version_part.split('.'):
                    if piece.isdigit():
                        parts.append(int(piece))
                    else:
                        break
                return tuple(parts)

            return max(pytorch_templates, key=version_key)

        return templates[0]

    def default_docker_template(self, executor_id: str) -> Template:
        """Resolve the best default template for an executor ID.

        Args:
            executor_id: Executor identifier returned by :meth:`ls`.

        Returns:
            :class:`Template` best suited for the executor.

        Raises:
            ValueError: If no matching executor or template exists.
        """
        executor = self.get_executor(executor_id)
        if not executor:
            raise ValueError(f"No executor found with id {executor_id}")

        default_images = self.get_default_images(executor.gpu_model, executor.driver_version)

        pytorch_image = next(
            (img for img in default_images if "pytorch" in img.get("docker_image", "").lower()), None
        )
        # set pytorch_image as first image
        if pytorch_image:
            default_images = [pytorch_image] + default_images
        for img in default_images:
            template = self.get_template_by_image_name(img.get("docker_image"), img.get("docker_image_tag"))
            if template:
                return template

        fallback = self._select_fallback_template()
        if fallback:
            return fallback

        raise ValueError("No templates available to use for executor")


    def templates(self, filter: Optional[str] = None, only_my: bool = False) -> List[Template]:
        """List available templates.

        Args:
            filter: Optional substring to filter by image or name.
            only_my: When ``True`` return only templates owned by the caller.

        Returns:
            List of :class:`Template`.
        """
        data = self._request("GET", "/templates").json()

        if only_my:
            user_id = self.get_my_user_id()
            data = [d for d in data if d.get("user_id") == user_id]

        templates = [
            Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
            for d in data
        ]
        if filter:
            filter_lower = filter.lower()
            templates = [
                t for t in templates
                if filter_lower in t.docker_image.lower() or filter_lower in t.name.lower()
            ]

        return templates


    def get_executor(self, executor: str) -> Optional[ExecutorInfo]:
        """Resolve an executor by ID.

        Args:
            executor: Executor ID string.

        Returns:
            Matching :class:`ExecutorInfo` or ``None`` if not found.
        """
        for e in self.ls():
            if e.id == executor:
                return e
        return None

    def _resolve_machine_name(self, gpu_short: str) -> Optional[str]:
        """Resolve a short GPU name to all matching full machine names from API.

        Args:
            gpu_short: Short GPU name like "A100", "H200", etc.

        Returns:
            Comma-separated string of all matching machine names, or None if not found.
        """
        try:
            available_machines = self._request("GET", "/machines").json()
            gpu_short_normalized = gpu_short.upper()
            matching_machines = []

            for machine in available_machines:
                machine_name = machine.get("name", "")
                # Check if the short name matches the extracted GPU type
                if extract_gpu_type(machine_name).upper() == gpu_short_normalized:
                    matching_machines.append(machine_name)

            # Return comma-separated list of all matches
            if matching_machines:
                return ",".join(matching_machines)
        except Exception:
            pass
        return None

    def gpu_types(self) -> set[str]:
        """Get list of available GPU types.

        Returns:
            Set of GPU type strings advertised by the API.
        """
        available_machines = self._request("GET", "/machines").json()
        gpu_types = {machine.get("name") or "" for machine in available_machines}
        return gpu_types

    def get_template(self, template_id: str) -> Optional[Template]:
        """Fetch a template by ID/HUID/name.

        Args:
            template_id: Template ID, HUID, or name to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        try:
            d = self._request("GET", f"/templates/{template_id}").json()
            return Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
        except Exception:
            return None

    def get_template_by_image_name(self, image_name: Optional[str] = None, image_tag: Optional[str] = None) -> Optional[Template]:
        """Fetch a template by its Docker image + tag.

        Args:
            image_name: Repository/image name.
            image_tag: Tag to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        templates = self.templates()
        for t in templates:
            if t.docker_image == image_name and t.docker_image_tag == image_tag:
                return t

    @contextmanager
    def ssh_connection(self, pod: PodInfo, timeout: int = 30):
        """SSH connection context manager.

        Args:
            pod: Pod whose SSH metadata is used.
            timeout: Connection timeout in seconds.

        Yields:
            An active ``paramiko.SSHClient``.
        """
        if not pod.ssh_cmd:
            raise ValueError(f"No SSH for pod {pod.name}")

        if not self.config.ssh_key_path:
            raise ValueError("No SSH key configured")

        # Parse SSH command
        parts = shlex.split(pod.ssh_cmd)
        user_host = parts[1]
        user, host = user_host.split("@")
        port = pod.ssh_port

        # Load SSH key
        key = None
        for key_type in [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]:
            try:
                key = key_type.from_private_key_file(str(self.config.ssh_key_path))
                break
            except (paramiko.SSHException, FileNotFoundError, PermissionError):
                continue

        if not key:
            raise ValueError("Could not load SSH key")

        # Connect
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, port=port, username=user, pkey=key, timeout=timeout)

        try:
            yield client
        finally:
            client.close()

    def _prep_command(self, command: str, env: Optional[Dict[str, str]] = None) -> str:
        """Prepare command with environment variables."""
        if env:
            env_str = " && ".join([f'export {k}="{v}"' for k, v in env.items()])
            return f"{env_str} && {command}"
        return command

    def exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute a shell command on a pod over SSH.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.

        Returns:
            Dict containing stdout, stderr, exit_code, and success flag.
        """
        command = self._prep_command(command, env)

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            return {
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace"),
                "exit_code": exit_code,
                "success": exit_code == 0
            }

    def stream_exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
    ) -> Generator[Dict[str, str], None, None]:
        """Execute a shell command and stream incremental output.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.

        Yields:
            Streaming output chunks as ``{"type": "stdout"|"stderr", "data": str}``.
        """
        command = self._prep_command(command, env)

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command, get_pty=True)
            stdin.close()

            channel = stdout.channel
            channel.settimeout(0.1)

            while not channel.closed or channel.recv_ready() or channel.recv_stderr_ready():
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stdout", "data": data}

                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stderr", "data": data}

    def exec_all(
        self,
        pods: List[PodInfo],
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
        max_workers: int = 10,
    ) -> List[Dict]:
        """Execute a shell command on multiple pods in parallel.

        Args:
            pods: List of pods to target.
            command: Shell command to run on each pod.
            env: Optional environment variables exported before each command.
            max_workers: Maximum number of SSH workers to spawn.

        Returns:
            List of result dictionaries mirroring :meth:`exec`.
        """
        def exec_single(pod: PodInfo):
            try:
                result = self.exec(pod, command=command, env=env)
                result["pod"] = pod.id
                return result
            except Exception as e:
                return {"pod": pod, "error": str(e), "success": False}

        with ThreadPoolExecutor(max_workers=min(max_workers, len(pods))) as executor:
            return list(executor.map(exec_single, pods))

    def wait_ready(
        self,
        pod: Union[str, PodInfo, Dict],
        *,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> Optional[PodInfo]:
        """Poll until a pod reports RUNNING + SSH metadata.

        Args:
            pod: Pod identifier, PodInfo, or dict with an ``id`` field.
            timeout: Maximum number of seconds to wait.
            poll_interval: Interval between successive ``ps`` calls.

        Returns:
            PodInfo when the pod is ready, otherwise ``None`` if timeout expires.
        """
        if isinstance(pod, PodInfo):
            pod_id = pod.id
        elif isinstance(pod, dict) and 'id' in pod:
            pod_id = pod['id']
        else:
            pod_id = pod

        start = time.time()
        while time.time() - start < timeout:
            fresh_pods = self.ps()
            current = next((p for p in fresh_pods if p.id == pod_id), None)

            if current and current.status.upper() == "RUNNING" and current.ssh_cmd:
                return current

            time.sleep(poll_interval)
        return None

    def scp(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a local file to a pod via SFTP."""
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.put(local, remote)
            sftp.close()

    def download(self, pod: PodInfo, *, remote: str, local: str) -> None:
        """Download a file from a pod via SFTP.

        Args:
            pod: The pod to download from.
            remote: Remote file path on the pod.
            local: Local destination path.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.get(remote, local)
            sftp.close()

    def upload(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a file to a pod via SFTP.

        This is an alias for :meth:`scp` for parity with the CLI.

        Args:
            pod: The pod to upload to.
            local: Local file path to upload.
            remote: Remote destination path on the pod.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        self.scp(pod, local=local, remote=remote)

    def ssh(self, pod: PodInfo) -> str:
        """Get SSH command string for connecting to a pod.

        Args:
            pod: The pod to generate SSH command for.

        Returns:
            SSH command string with the configured SSH key path.

        Raises:
            ValueError: If SSH is not configured for the pod or no SSH key path is set.
        """
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        return pod.ssh_cmd.replace("ssh ", f"ssh -i {self.config.ssh_key_path} ")

    def rsync(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Sync directories with rsync.

        Args:
            pod: Pod to sync.
            local: Local path or directory (rsync source).
            remote: Remote path on the pod.

        Raises:
            RuntimeError: If the rsync command fails.
        """
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        ssh_cmd = f"ssh -i {self.config.ssh_key_path} -p {pod.ssh_port} -o StrictHostKeyChecking=no"
        cmd = ["rsync", "-avz", "-e", ssh_cmd, local,  f"{pod.username}@{pod.host}:{remote}"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Rsync failed: {result.stderr}")
    
    def switch_template(self, pod: PodInfo, *, template_id: str) -> PodInfo:
        """Switch the template of a running pod.
        
        Args:
            pod: Pod to update.
            template_id: ID of the template to switch to.
            
        Returns:
            PodInfo object with updated pod information.
        """
        payload = {
            "template_id": template_id
        }
        
        response = self._request("PUT", f"/pods/{pod.id}/switch-template", json=payload).json()
        
        # Parse the response into a PodInfo object
        return PodInfo(
            id=pod.id,  # Keep the original pod ID
            name=response.get("pod_name", pod.name),
            status=response.get("status", "PENDING"),
            huid=pod.huid,  # Keep the original HUID
            gpu_count=int(response.get("gpu_count", 0)),
            price=response.get("price", 0.0),
            ssh_cmd=response.get("ssh_connect_cmd"),
            ports=response.get("ports_mapping", {}),
            created_at=response.get("created_at", ""),
            updated_at=response.get("updated_at", ""),
            executor=ExecutorInfo(
                id=response.get("executor_id", ""),
                huid="",
                machine_name="",
                gpu_type=response.get("gpu_name", ""),
                gpu_count=int(response.get("gpu_count", 0) or 0),
                available_gpu_count=0,
                price_per_hour=0.0,
                price_per_gpu_hour=0.0,
                location={},
                specs={},
                status="",
                docker_in_docker=False
            ) if response.get("executor_id") else None,
            template={"id": response.get("template_id", template_id)},
            removal_scheduled_at=None,
            jupyter_installation_status=None,
            jupyter_url=None
        )

    
    def create_template(
        self,
        name: str,
        docker_image: str,
        docker_image_digest: str = "",
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Create a new template.

        Args:
            name: Friendly template name.
            docker_image: Image repository (e.g., ``"daturaai/pytorch"``).
            docker_image_digest: Digest string for pinning (defaults to empty string).
            docker_image_tag: Image tag (defaults to ``"latest"``).
            ports: Internal ports to expose (defaults to ``[22, 8000]``).
            start_command: Optional command executed on container start.
            **kwargs: Additional template fields:
                - category (str): Template category (defaults to ``"UBUNTU"``).
                - is_private (bool): Whether template is private (defaults to ``True``).
                - volumes (List[str]): Volume mount paths (defaults to ``["/workspace"]``).
                - description (str): Template description.
                - environment (Dict[str, str]): Environment variables.
                - entrypoint (str): Container entrypoint.

        Returns:
            Newly created :class:`Template`.
        """
        payload = {
            "name": name,
            "docker_image": docker_image,
            "docker_image_digest": docker_image_digest,
            "docker_image_tag": docker_image_tag,
            "internal_ports": ports or [22, 8000],
            "startup_commands": start_command or "",
            "category": kwargs.get("category", "UBUNTU"),
            "container_start_immediately": kwargs.get("container_start_immediately", True),
            "description": kwargs.get("description", name),
            "entrypoint": kwargs.get("entrypoint", ""),
            "environment": kwargs.get("environment", {}),
            "is_private": kwargs.get("is_private", True),
            "readme": kwargs.get("readme", name),
            "volumes": kwargs.get("volumes", ["/workspace"]),
        }

        response = self._request("POST", "/templates", json=payload).json()
        return Template(
            id=response.get("id", ""),
            huid=generate_huid(response.get("id", "")),
            name=response.get("name", ""),
            docker_image=response.get("docker_image", ""),
            docker_image_tag=response.get("docker_image_tag", "latest"),
            category=response.get("category", "general"),
            status=response.get("status", "unknown"),
        )

    def wait_template_ready(self, template_id: str, timeout: int = 300) -> Optional[Template]:
        """Wait for template verification to complete.

        Args:
            template_id: Template identifier.
            timeout: Maximum seconds to wait.

        Returns:
            Template when verification succeeds, otherwise ``None`` if the timeout expires.

        Raises:
            LiumError: If template verification fails.
        """

        start = time.time()
        while time.time() - start < timeout:
            templates = self.templates(only_my=True)
            current = next((t for t in templates if t.id == template_id), None)

            if current:
                status = current.status.upper()
                if status == "VERIFY_SUCCESS":
                    return current
                elif status == "VERIFY_FAILED":
                    raise LiumError(f"Template verification failed: {current.name}")

            time.sleep(10)
        return None

    def get_my_user_id(self) -> str:
        """Get the current user's ID.

        Returns:
            The ID returned by ``/users/me``.
        """
        return self._request("GET", "/users/me").json()["id"]

    def update_template(
        self,
        template_id: str,
        name: str,
        docker_image: str,
        docker_image_digest: str,
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Update an existing template owned by the caller.

        Args:
            template_id: Template identifier.
            name: Friendly name.
            docker_image: Image repository.
            docker_image_digest: Optional digest.
            docker_image_tag: Image tag.
            ports: Internal ports to expose.
            start_command: Startup command.
            **kwargs: Additional override fields.

        Returns:
            Updated :class:`Template`.

        Raises:
            ValueError: If the template is missing or not owned by the caller.
        """
        templates = self._request("GET", "/templates").json()
        current = next((t for t in templates if t["id"] == template_id), None)

        if not current:
            raise ValueError(f"Template with ID {template_id} not found")

        if current.get("user_id") != self.get_my_user_id():
            raise ValueError(f"Cannot update template {template_id}: not owned by current user")

        payload = current.copy()
        payload.update({
                "name": name,
                "docker_image": docker_image,
                "docker_image_digest": docker_image_digest,
                "docker_image_tag": docker_image_tag,
                "internal_ports": ports or [22, 8000],
                "startup_commands": start_command or "",
                "category": kwargs.get("category", payload.get("category", "UBUNTU")),
                "container_start_immediately": kwargs.get("container_start_immediately", payload.get("container_start_immediately", True)),
                "description": kwargs.get("description", payload.get("description", name)),
                "entrypoint": kwargs.get("entrypoint", payload.get("entrypoint", "")),
                "environment": kwargs.get("environment", payload.get("environment", {})),
                "is_private": kwargs.get("is_private", payload.get("is_private", False)),
                "readme": kwargs.get("readme", payload.get("readme", name)),
                "volumes": kwargs.get("volumes", payload.get("volumes", [])),
        })

        resp = self._request("PUT", f"/templates/{template_id}", json=payload).json()
        return Template(
            id=template_id,
            huid=generate_huid(template_id),
            name=payload['name'],
            docker_image=payload['docker_image'],
            docker_image_tag=payload['docker_image_tag'],
            category=payload['category'],
            status=resp.get("status", "unknown"),
        )


    def wallets(self) -> List[Dict[str, Any]]:
        """Get the caller's configured funding wallets.

        Returns:
            Raw wallet records returned by the pay API.
        """
        user = self._request("GET", "/users/me").json()
        pay_headers = {"X-API-KEY": "6RhXQ788J9BdnqeLua8z7ZSkXBDahclxhwjMB17qW1M"}
        resp = self._request(
            "GET",
            f"/wallet/available-wallets/{user['stripe_customer_id']}",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
        )
        return resp.json()

    def add_wallet(self, bt_wallet: Any) -> None:
        """Link a Bittensor wallet with the user account.

        Args:
            bt_wallet: Wallet object exposing ``coldkey``/``coldkeypub`` for signing.

        Raises:
            LiumError: If verification or wallet polling fails.
        """
        pay_headers = {"X-API-KEY": "6RhXQ788J9BdnqeLua8z7ZSkXBDahclxhwjMB17qW1M"}
        access_key = self._request(
            "GET", "/token/generate", base_url=self.config.base_pay_url, headers=pay_headers
        ).json()["access_key"]
        sig = bt_wallet.coldkey.sign(access_key.encode()).hex()
        create_transfer_response = self._request("POST", "/tao/create-transfer", json={"amount": 10})
        redirect_url = create_transfer_response.json()["url"]
        
        # Parse URL parameters elegantly
        parsed_url = urlparse(redirect_url)
        params = parse_qs(parsed_url.query)
        app_id = params["app_id"][0]
        stripe_customer_id = params["customer_id"][0]

        verify_response = self._request(
            "POST",
            "/token/verify",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
            json={
                "coldkey_address": bt_wallet.coldkeypub.ss58_address,
                "access_key": access_key,
                "signature": sig,
                "stripe_customer_id": stripe_customer_id,
                "application_id": app_id,
            },
        )
        if verify_response.json()["status"].lower() != "ok":
            raise LiumError(f"Failed to add wallet: {verify_response.text}")

        for i in range(5):
            wallets = [w.get('wallet_hash', '') for w in self.wallets()]
            if bt_wallet.coldkeypub.ss58_address in wallets:
                return
            time.sleep(2)
        raise LiumError("Failed to add wallet. Wallet not found after 5 attempts.")

    def backup_create(
        self,
        pod: PodInfo,
        *,
        path: str = "/home",
        frequency_hours: int = 6,
        retention_days: int = 7,
    ) -> BackupConfig:
        """Create or replace a backup configuration for a pod.

        Args:
            pod: Pod to configure.
            path: Filesystem path to back up.
            frequency_hours: Backup interval in hours.
            retention_days: Retention period in days.

        Returns:
            Created :class:`BackupConfig`.
        """
        payload = {
            "pod_id": pod.id,
            "backup_frequency_hours": frequency_hours,
            "retention_days": retention_days,
            "backup_path": path
        }
        
        response = self._request("POST", "/backup-configs", json=payload).json()
        
        return self._dict_to_backup_config(response)

    def backup_now(
        self,
        pod: PodInfo,
        *,
        name: str,
        description: str = "",
    ) -> Dict[str, Any]:
        """Trigger an immediate backup for a pod.

        Args:
            pod: Pod to back up.
            name: Backup name.
            description: Optional description.

        Returns:
            API response payload from the run-now endpoint.
        """
        payload = {
            "name": name,
            "description": description
        }
        
        return self._request("POST", f"/pods/{pod.id}/backup", json=payload).json()

    def backup_config(self, pod: PodInfo) -> Optional[BackupConfig]:
        """Return the backup configuration for a pod if one exists.

        Args:
            pod: Pod to inspect.

        Returns:
            :class:`BackupConfig` if present, otherwise ``None``.
        """
        if not pod.executor:
            raise ValueError(f"Pod {pod.name} has no executor information")
        try:
            response = self._request("GET", f"/backup-configs/pod/{pod.executor.id}").json()
            return self._dict_to_backup_config(response) if response else None
        except LiumNotFoundError:
            # No backup config exists for this pod
            return None
    
    def backup_list(self) -> List[BackupConfig]:
        """List all backup configurations across all pods.

        Returns:
            List of :class:`BackupConfig`.
        """
        configs = self._request("GET", "/backup-configs").json()
        return [self._dict_to_backup_config(c) for c in configs]

    def backup_logs(self, pod: PodInfo) -> List[BackupLog]:
        """Get recent backup logs for a pod.

        Args:
            pod: Pod to inspect.

        Returns:
            List of :class:`BackupLog` entries (possibly empty).
        """
        if not pod.executor:
            raise ValueError(f"Pod {pod.name} has no executor information")
        
        try:
            response = self._request("GET", f"/backup-logs/pod/{pod.executor.id}").json()
            
            # Handle paginated response - extract items from the response
            if isinstance(response, dict) and 'items' in response:
                logs = response['items']
            else:
                # Fallback for non-paginated response
                logs = response if isinstance(response, list) else []
            
            return [self._dict_to_backup_log(log) for log in logs]
        except LiumNotFoundError:
            # No backup logs exist for this pod, return empty list
            return []

    def backup_delete(self, config_id: str) -> Dict[str, Any]:
        """Delete a backup configuration by ID.

        Args:
            config_id: Backup configuration identifier.

        Returns:
            API response payload.
        """
        return self._request("DELETE", f"/backup-configs/{config_id}").json()
    
    def restore(
        self,
        pod: PodInfo,
        *,
        backup_id: str,
        restore_path: str = "/root",
    ) -> Dict[str, Any]:
        """Restore a backup to a pod.
        
        Args:
            pod: Pod to restore to.
            backup_id: ID of the backup to restore.
            restore_path: Path where to restore the backup (default: /root).
            
        Returns:
            Response from the restore API.
        """
        payload = {
            "backup_id": backup_id,
            "restore_path": restore_path
        }
        
        return self._request("POST", f"/pods/{pod.id}/restore", json=payload).json()

    def balance(self) -> float:
        """Get current account balance.

        Returns:
            Floating-point balance value reported by ``/users/me``.
        """
        return float(self._request("GET", "/users/me").json().get("balance", 0))

    def volumes(self) -> List[VolumeInfo]:
        """List all volumes for the current user.

        Returns:
            List of :class:`VolumeInfo`.
        """
        data = self._request("GET", "/volumes").json()
        return [self._dict_to_volume_info(v) for v in data]

    def volume(self, volume_id: str) -> VolumeInfo:
        """Get a specific volume by ID.

        Args:
            volume_id: Volume identifier.

        Returns:
            :class:`VolumeInfo` for the requested volume.
        """
        response = self._request("GET", f"/volumes/{volume_id}").json()
        return self._dict_to_volume_info(response)

    def volume_create(self, name: str, *, description: str = "") -> VolumeInfo:
        """Create a new volume.

        Args:
            name: Volume name.
            description: Optional description.

        Returns:
            Created :class:`VolumeInfo`.
        """
        payload = {"name": name, "description": description}
        response = self._request("POST", "/volumes", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_update(self, volume_id: str, *, name: Optional[str] = None, description: Optional[str] = None) -> VolumeInfo:
        """Update a volume's metadata.

        Args:
            volume_id: Volume identifier.
            name: Optional new name.
            description: Optional description.

        Returns:
            Updated :class:`VolumeInfo`.

        Raises:
            ValueError: If neither ``name`` nor ``description`` is provided.
        """
        payload = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if not payload:
            raise ValueError("At least one of name or description must be provided")
        response = self._request("PUT", f"/volumes/{volume_id}", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_delete(self, volume_id: str) -> Dict[str, Any]:
        """Delete a volume.

        Args:
            volume_id: Volume identifier.

        Returns:
            API response payload from the delete request.
        """
        return self._request("DELETE", f"/volumes/{volume_id}").json()

    def schedule_termination(self, pod: PodInfo, *, termination_time: str) -> Dict[str, Any]:
        """Schedule a pod for automatic termination at a future date and time.

        Args:
            pod: Pod to schedule
            termination_time: ISO 8601 formatted datetime string (e.g., "2025-10-17T15:30:00Z")

        Returns:
            Response from the schedule termination API
        """
        payload = {"removal_scheduled_at": termination_time}
        return self._request("POST", f"/pods/{pod.id}/schedule-removal", json=payload).json()

    def cancel_scheduled_termination(self, pod: PodInfo) -> Dict[str, Any]:
        """Cancel a scheduled termination for a pod.

        Args:
            pod: Pod to cancel the schedule for

        Returns:
            Response from the cancel scheduled termination API
        """
        return self._request("DELETE", f"/pods/{pod.id}/schedule-removal").json()

    def install_jupyter(self, pod: PodInfo, *, jupyter_internal_port: int) -> Dict[str, Any]:
        """Install Jupyter Notebook on a pod.

        Args:
            pod: Pod to install Jupyter on
            jupyter_internal_port: Internal port for Jupyter Notebook

        Returns:
            Response from the install Jupyter API
        """
        payload = {"jupyter_internal_port": jupyter_internal_port}
        return self._request("POST", f"/pods/{pod.id}/install-jupyter", json=payload).json()
