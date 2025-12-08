"""Datamodels used across the Lium SDK."""

from dataclasses import dataclass
import re
from typing import Dict, List, Optional


@dataclass
class ExecutorInfo:
    id: str
    huid: str
    machine_name: str
    gpu_type: str
    gpu_count: int
    available_gpu_count: int
    price_per_gpu_hour: float
    location: Dict
    specs: Dict
    status: str
    docker_in_docker: bool
    ip: str
    available_port_count: Optional[int] = None
    min_gpu_count_for_rental: int | None = None

    @property
    def driver_version(self) -> str:
        """Extract GPU driver version from specs."""
        return self.specs.get('gpu', {}).get('driver', '')

    @property
    def gpu_model(self) -> str:
        """Extract GPU model name from specs."""
        gpu_details = self.specs.get('gpu', {}).get('details', [])
        return gpu_details[0].get('name', '') if gpu_details else ''


@dataclass
class PodInfo:
    id: str
    name: str
    status: str
    huid: str
    ssh_cmd: Optional[str]
    ports: Dict
    created_at: str
    updated_at: str
    gpu_count: int
    price: float
    executor: Optional[ExecutorInfo]
    template: Dict
    removal_scheduled_at: Optional[str]
    jupyter_installation_status: Optional[str]
    jupyter_url: Optional[str]

    @property
    def host(self) -> Optional[str]:
        return (re.findall(r'@(\S+)', self.ssh_cmd) or [None])[0] if self.ssh_cmd else None

    @property
    def username(self) -> Optional[str]:
        return (re.findall(r'ssh (\S+)@', self.ssh_cmd) or [None])[0] if self.ssh_cmd else None

    @property
    def ssh_port(self) -> int:
        """Extract SSH port from command."""
        if not self.ssh_cmd or '-p ' not in self.ssh_cmd:
            return 22
        return int(self.ssh_cmd.split('-p ')[1].split()[0])


@dataclass
class Template:
    """Template information."""
    id: str
    name: str
    huid: str
    docker_image: str
    docker_image_tag: str
    category: str
    status: str


@dataclass
class BackupConfig:
    """Backup configuration information."""
    id: str
    huid: str
    pod_executor_id: str
    backup_frequency_hours: int
    retention_days: int
    backup_path: str
    is_active: bool
    created_at: str
    updated_at: Optional[str] = None


@dataclass
class BackupLog:
    """Backup log information."""
    id: str
    huid: str
    backup_config_id: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    progress: Optional[float] = None
    backup_volume_id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class VolumeInfo:
    """Volume information."""
    id: str
    huid: str
    name: str
    description: str
    created_at: str
    updated_at: Optional[str] = None
    current_size_bytes: int = 0
    current_file_count: int = 0
    current_size_gb: float = 0.0
    current_size_mb: float = 0.0
    last_metrics_update: Optional[str] = None


__all__ = [
    "ExecutorInfo",
    "PodInfo",
    "Template",
    "BackupConfig",
    "BackupLog",
    "VolumeInfo",
]
