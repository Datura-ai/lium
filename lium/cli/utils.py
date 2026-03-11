"""CLI utilities and decorators."""
from functools import wraps
from contextlib import contextmanager
from typing import List, Dict, Any, Tuple, Optional, Callable, TypeVar
import json
from pathlib import Path
from lium.cli.settings import config
from datetime import datetime
from rich.status import Status
from lium.sdk import LiumError, ExecutorInfo, PodInfo,Lium
from .themed_console import ThemedConsole
from dataclasses import dataclass
from rich.prompt import Prompt

T = TypeVar("T")

console = ThemedConsole()


# Text formatting helpers

def mid_ellipsize(s: str, width: int = 28) -> str:
    """Truncate string with middle ellipsis if too long."""
    if not s:
        return "—"
    if len(s) <= width:
        return s
    keep = width - 1
    left = keep // 2
    right = keep - left
    return f"{s[:left]}…{s[-right:]}"


def parse_timestamp(timestamp: str) -> Optional[datetime]:
    """Parse ISO format timestamp."""
    from datetime import datetime, timezone
    try:
        if timestamp.endswith('Z'):
            return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif '+' not in timestamp and '-' not in timestamp[10:]:
            return datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        else:
            return datetime.fromisoformat(timestamp)
    except (ValueError, AttributeError):
        return None


def format_date(timestamp: str) -> str:
    """Format timestamp as relative or absolute date."""
    from datetime import datetime, timezone
    if not timestamp:
        return "—"

    dt = parse_timestamp(timestamp)
    if not dt:
        return "—"

    now = datetime.now(timezone.utc)
    delta = now - dt

    # If less than 24 hours, show relative time
    if delta.total_seconds() < 86400:  # 24 hours
        hours = delta.total_seconds() / 3600
        if hours < 1:
            mins = delta.total_seconds() / 60
            return f"{mins:.0f}m ago"
        else:
            return f"{hours:.1f}h ago"
    # If less than 7 days, show days
    elif delta.days < 7:
        return f"{delta.days}d ago"
    # Otherwise show date
    else:
        return dt.strftime("%Y-%m-%d")


def _prompt_value(
    prompt_text: str,
    default_value: T,
    value: T,
    cast: Callable[[str], T],
    validate: Callable[[T], bool],
) -> T:
    """Loop: Enter -> default, invalid -> reprompt until valid."""
    default_str = str(default_value)
    if value != default_value:
        return value
    while True:
        raw = Prompt.ask(prompt_text, default=default_str)
        # Empty/Enter -> use default
        if raw.strip() == "":
            return default_value
        try:
            value = cast(raw)
        except Exception:
            console.error("Invalid value, please try again")
            continue
        if not validate(value):
            console.error("Invalid value, please try again")
            continue
        return value


@dataclass
class BackupParams:
    """Backup configuration parameters."""
    enabled: bool = False
    path: str = config.default_backup_path
    frequency: int = config.default_backup_frequency  # hours
    retention: int = config.default_backup_retention  # days
    
    def validate(self) -> None:
        """Validate backup parameters."""
        if not self.enabled:
            return
            
        if not self.path.startswith('/'):
            raise ValueError(f"Backup path must be absolute (start with /), got: {self.path}")
        
        if self.frequency <= 0:
            raise ValueError(f"Backup frequency must be positive, got: {self.frequency}")
        
        if self.retention <= 0:
            raise ValueError(f"Backup retention must be positive, got: {self.retention}")
    
    def display_info(self) -> str:
        """Return formatted display info for backup configuration."""
        if not self.enabled:
            return "Backup: disabled"
        
        freq_display = f"{self.frequency}h" if self.frequency != 24 else "daily"
        ret_display = f"{self.retention} days" if self.retention != 7 else "1 week"
        
        return f"Backup: {self.path} every {freq_display}, retained for {ret_display}"


@contextmanager
def loading_status(message: str, success_message: str = ""):
    """Universal context manager to show loading status."""
    status = Status(f"{console.get_styled(message + '...', 'info')}", console=console)
    status.start()
    try:
        yield
        if success_message:
            console.success(f"✓ {success_message}")
    except Exception as e:
        console.error(f"✗ Failed: {e}")
        raise
    finally:
        status.stop()


def _update_spinner_display(step_prefix: str, message: str, start_time: float, running_flag):
    """Internal function to update spinner display with time."""
    import time
    import sys
    
    # Spinner characters (dots spinner)
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spinner_index = 0
    
    # Get green color code from console theme
    green_color = console.theme.get('success', 'green')
    # Convert Rich color to ANSI escape sequence
    if green_color == 'green':
        green_code = '\033[32m'  # ANSI green
    else:
        green_code = '\033[32m'  # fallback to green
    reset_code = '\033[0m'  # ANSI reset
    
    while running_flag[0]:  # Use list for mutable reference
        elapsed = time.time() - start_time
        spinner_char = spinner_chars[spinner_index % len(spinner_chars)]
        
        # Use carriage return to overwrite the line smoothly with green spinner
        line = f"{step_prefix}{message}... {green_code}{spinner_char}{reset_code} ({elapsed:.1f}s)"
        sys.stdout.write(f"\r{line}")
        sys.stdout.flush()
        spinner_index += 1
        time.sleep(0.1)


def _handle_step_completion(step_prefix: str, message: str, elapsed: float, exception: Optional[Exception] = None):
    """Internal function to handle step completion display."""
    import sys
    
    # Clear the line and print final result
    sys.stdout.write('\r\033[K')  # Clear entire line
    
    if exception is None:
        # Success case
        done_styled = console.get_styled("done", 'success')
        console.print(f"{step_prefix}{message}... {done_styled} ({elapsed:.1f}s)", highlight=False)
    else:
        # General failure case
        failed_styled = console.get_styled("failed", 'error')
        console.print(f"{step_prefix}{message}... {failed_styled} ({elapsed:.1f}s)", highlight=False)


@contextmanager
def timed_step_status(step: int = 0, total_steps: int = 0, message: str = ""):
    """Context manager to show timed step status like [1/3] Renting machine... ⠋ (3.2s) -> [1/3] Renting machine... done (3.2s)."""
    import time
    import threading
    import sys
    
    start_time = time.time()
    # Only show step prefix if steps > 0 (with bullet for visual separation)
    step_prefix = f"● [{step}/{total_steps}] " if step > 0 and total_steps > 0 else ""
    running = [True]  # Use list for mutable reference
    
    # Hide cursor during animation
    sys.stdout.write('\033[?25l')  # Hide cursor
    sys.stdout.flush()
    
    # Start the update thread
    update_thread = threading.Thread(target=_update_spinner_display, args=(step_prefix, message, start_time, running), daemon=True)
    update_thread.start()
    
    try:
        yield
        # Stop the update and show success
        running[0] = False
        update_thread.join(timeout=0.1)
        
        # Show cursor again
        sys.stdout.write('\033[?25h')  # Show cursor
        
        elapsed = time.time() - start_time
        _handle_step_completion(step_prefix, message, elapsed)
        
    except Exception as e:
        # Stop the update and show appropriate message
        running[0] = False
        update_thread.join(timeout=0.1)
        
        # Show cursor again
        sys.stdout.write('\033[?25h')  # Show cursor
        
        elapsed = time.time() - start_time
        _handle_step_completion(step_prefix, message, elapsed, e)
        raise


def handle_errors(func):
    """Decorator to handle CLI errors gracefully."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError as e:
            # Check if it's the API key error from SDK
            if "No API key found" in str(e):
                console.error("No API key configured")
                console.warning("Please run 'lium init' to set up your API key")
                console.dim("Or set LIUM_API_KEY environment variable")
            else:
                console.error(f"Error: {e}")
        except LiumError as e:
            console.error(f"Error: {e}")
        except Exception as e:
            console.error(f"Unexpected error: {e}")
    return wrapper


def extract_executor_metrics(executor: ExecutorInfo) -> Dict[str, float]:
    """Extract relevant metrics from an executor for Pareto comparison."""
    specs = executor.specs or {}
    
    # GPU metrics
    gpu_info = specs.get("gpu", {})
    gpu_details = gpu_info.get("details", [{}])[0] if gpu_info.get("details") else {}
    
    # System metrics
    ram_data = specs.get("ram", {})
    disk_data = specs.get("hard_disk", {})
    network = specs.get("network", {})
    
    # Location preference (US gets a bonus)
    location = executor.location or {}
    country = location.get("country", "").upper()
    country_code = location.get("country_code", "").upper()
    is_us = country == "UNITED STATES" or country_code == "US"
    
    return {
        'price_per_gpu_hour': executor.price_per_gpu_hour or float('inf'),
        'vram_gb': (gpu_details.get("capacity", 0) / 1024) if gpu_details else 0,  # MiB to GB
        'ram_gb': (ram_data.get("total", 0) / (1024 * 1024)) if ram_data else 0,  # KB to GB
        'disk_gb': (disk_data.get("total", 0) / (1024 * 1024)) if disk_data else 0,  # KB to GB
        'pcie_speed': gpu_details.get("pcie_speed", 0),
        'memory_bandwidth': gpu_details.get("memory_speed", 0),
        'tflops': gpu_details.get("graphics_speed", 0),
        'net_up': network.get("upload_speed") or 0,
        'net_down': network.get("download_speed") or 0,
        'location_score': 1.0 if is_us else 0.0,  # US locations get higher score
        'total_bandwidth': (network.get("upload_speed") or 0) + (network.get("download_speed") or 0),  # Combined bandwidth
    }


def dominates(metrics_a: Dict[str, float], metrics_b: Dict[str, float]) -> bool:
    """Check if executor A dominates executor B in Pareto sense."""
    # Metrics to minimize (lower is better)
    minimize_metrics = {'price_per_gpu_hour'}
    
    # Priority metrics when prices are equal
    priority_metrics = ['total_bandwidth', 'location_score', 'net_down', 'net_up']
    
    price_a = metrics_a.get('price_per_gpu_hour', float('inf'))
    price_b = metrics_b.get('price_per_gpu_hour', float('inf'))
    
    # Special handling when prices are equal (common with GPU filtering)
    if abs(price_a - price_b) < 0.01:  # Prices are effectively equal
        # Compare based on priority metrics
        for metric in priority_metrics:
            val_a = metrics_a.get(metric, 0) or 0
            val_b = metrics_b.get(metric, 0) or 0
            
            # Significant difference threshold (10% for bandwidth, any diff for location)
            threshold = 0.1 * max(val_a, val_b) if metric != 'location_score' else 0
            
            if val_a > val_b + threshold:
                # A is significantly better in this priority metric
                return True
            elif val_b > val_a + threshold:
                # B is significantly better in this priority metric
                return False
        
        # If all priority metrics are similar, compare all metrics
        at_least_one_better = False
        for metric in metrics_a:
            if metric in priority_metrics:
                continue  # Already checked
            
            val_a = metrics_a[metric] or 0
            val_b = metrics_b.get(metric, 0) or 0
            
            if val_a > val_b:
                at_least_one_better = True
            elif val_a < val_b:
                return False
        
        return at_least_one_better
    
    # Standard Pareto domination when prices differ
    at_least_one_better = False
    
    for metric in metrics_a:
        val_a = metrics_a[metric] or 0
        val_b = metrics_b.get(metric, 0) or 0

        if metric in minimize_metrics:
            # For minimize metrics, A is better if it's lower
            if val_a < val_b:
                at_least_one_better = True
            elif val_a > val_b:
                return False  # B is better in this metric
        else:
            # For maximize metrics, A is better if it's higher
            if val_a > val_b:
                at_least_one_better = True
            elif val_a < val_b:
                return False  # B is better in this metric
    
    return at_least_one_better


def calculate_pareto_frontier(executors: List[ExecutorInfo]) -> List[bool]:
    """Calculate which executors are on the Pareto frontier.
    
    Returns a list of booleans indicating if each executor is Pareto-optimal.
    """
    # Extract metrics for all executors
    metrics_list = [extract_executor_metrics(e) for e in executors]
    
    # Mark each executor as Pareto-optimal or not
    is_pareto = []
    for i, metrics_i in enumerate(metrics_list):
        dominated = False
        for j, metrics_j in enumerate(metrics_list):
            if i != j and dominates(metrics_j, metrics_i):
                dominated = True
                break
        is_pareto.append(not dominated)
    
    return is_pareto


def store_executor_selection(executors: List[ExecutorInfo]) -> None:
    """Store the last executor selection for index-based selection."""
    from lium.cli.settings import config
    
    selection_data = {
        'timestamp': datetime.now().isoformat(),
        'executors': []
    }
    
    for executor in executors:
        selection_data['executors'].append({
            'id': executor.id,
            'huid': executor.huid,
            'gpu_type': executor.gpu_type,
            'gpu_count': executor.gpu_count,
            'available_gpu_count': executor.available_gpu_count,
            'price_per_gpu_hour': executor.price_per_gpu_hour,
            'min_gpu_count_for_rental': executor.min_gpu_count_for_rental,
            'location': executor.location.get('country', 'Unknown') if executor.location else 'Unknown',
            'status': executor.status,
            'docker_in_docker': executor.docker_in_docker,
            'ip': executor.ip,
            'available_port_count': executor.available_port_count,
        })
    
    # Store in config directory
    config_file = config.config_dir / "last_selection.json"
    with open(config_file, 'w') as f:
        json.dump(selection_data, f, indent=2)


def get_last_executor_selection() -> Optional[Dict[str, Any]]:
    """Retrieve the last executor selection."""
    from lium.cli.settings import config
    
    config_file = config.config_dir / "last_selection.json"
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def store_volume_selection(volumes: List) -> None:
    """Store the last volume selection for HUID-based lookup."""
    from lium.cli.settings import config

    selection_data = {
        'timestamp': datetime.now().isoformat(),
        'volumes': []
    }

    for volume in volumes:
        # Handle VolumeInfo objects
        selection_data['volumes'].append({
            'id': volume.id,
            'huid': volume.huid,
            'name': volume.name,
            'description': volume.description,
            'current_size_gb': volume.current_size_gb,
        })

    # Store in config directory
    config_file = config.config_dir / "last_volumes.json"
    with open(config_file, 'w') as f:
        json.dump(selection_data, f, indent=2)


def get_last_volume_selection() -> Optional[Dict[str, Any]]:
    """Retrieve the last volume selection."""
    from lium.cli.settings import config

    config_file = config.config_dir / "last_volumes.json"
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def resolve_volume_huid(huid: str) -> Optional[str]:
    """
    Resolve volume HUID to database ID from cached selection.
    Returns database ID or None if not found.
    """
    last_selection = get_last_volume_selection()
    if not last_selection:
        return None

    volumes = last_selection.get('volumes', [])
    for volume in volumes:
        if volume.get('huid') == huid:
            return volume.get('id')

    return None


def parse_volume_spec(volume_spec: str) -> Tuple[Optional[str], Optional[Dict[str, str]], Optional[str]]:
    """
    Parse volume specification.

    Formats:
      - id:<HUID>                        -> resolve HUID to database ID
      - new[:name=X[,desc=Y]]            -> create new volume

    Returns:
        (volume_id, create_params, error_message)
        - volume_id: existing volume database ID
        - create_params: dict with 'name' and 'description' for new volume
        - error_message: error description if parsing failed
    """
    spec = volume_spec.strip()

    # Format: id:<HUID>
    if spec.startswith('id:'):
        huid = spec[3:].strip()
        if not huid:
            return None, None, "Volume HUID is missing after 'id:'"

        volume_id = resolve_volume_huid(huid)
        if not volume_id:
            return None, None, f"Volume with HUID '{huid}' not found. Run 'lium volumes' first."

        return volume_id, None, None

    # Format: new[:name=X,desc=Y] or just new
    if spec.startswith('new'):
        create_params = {'name': '', 'description': ''}

        # Check if there are parameters
        if len(spec) > 3:
            if not spec[3] == ':':
                return None, None, f"Invalid format: expected 'new' or 'new:name=...' but got '{spec}'"

            params_str = spec[4:].strip()
            if params_str:
                # Parse key=value pairs
                for param in params_str.split(','):
                    param = param.strip()
                    if '=' not in param:
                        return None, None, f"Invalid parameter format: '{param}'. Expected 'key=value'"

                    key, value = param.split('=', 1)
                    key = key.strip()
                    value = value.strip()

                    if key == 'name':
                        create_params['name'] = value
                    elif key == 'desc':
                        create_params['description'] = value
                    else:
                        return None, None, f"Unknown parameter: '{key}'. Use 'name' or 'desc'"

        # Name is required for new volumes
        if not create_params['name']:
            return None, None, "Volume name is required. Use 'new:name=<NAME>' or 'new:name=<NAME>,desc=<DESC>'"

        return None, create_params, None

    # Unknown format
    return None, None, f"Invalid volume format: '{spec}'. Use 'id:<HUID>' or 'new:name=<NAME>[,desc=<DESC>]'"


def resolve_executor_indices(indices: List[str]) -> Tuple[List[str], Optional[str]]:
    """
    Resolve executor indices from the last selection.
    Returns (resolved_executor_ids, error_message)
    """
    last_selection = get_last_executor_selection()
    if not last_selection:
        return [], None
    
    executors = last_selection.get('executors', [])
    if not executors:
        return [], "No executors in last selection."
    
    resolved_ids = []
    failed_resolutions = []
    
    for index_str in indices:
        try:
            index = int(index_str)
            if 1 <= index <= len(executors):
                executor_data = executors[index - 1]
                resolved_ids.append(executor_data['id'])
            else:
                failed_resolutions.append(f"Index {index_str} is out of range (1..{len(executors)}). Try: lium ls")
        except ValueError:
            failed_resolutions.append(f"{index_str} (not a valid index)")
    
    error_msg = None
    if failed_resolutions:
        error_msg = f"Could not resolve indices: {', '.join(failed_resolutions)}"
    
    return resolved_ids, error_msg


def parse_targets(targets: str, all_pods: List[PodInfo]) -> List[PodInfo]:
    """Parse target specification and return matching pods."""
    if targets.lower() == "all":
        return all_pods
    
    selected = []
    for target in targets.split(","):
        target = target.strip()
        
        # Try as index (1-based from ps output)
        try:
            idx = int(target) - 1
            if 0 <= idx < len(all_pods):
                selected.append(all_pods[idx])
                continue
        except ValueError:
            pass
        
        # Try as pod ID/name/huid
        for pod in all_pods:
            if target in (pod.id, pod.name, pod.huid):
                selected.append(pod)
                break
    
    return selected


def wait_ready_no_timeout(lium_client, pod_id: str):
    """Wait indefinitely for pod to be ready (RUNNING with SSH)."""
    import time
    
    while True:
        fresh_pods = lium_client.ps()
        pod = next((p for p in fresh_pods if p.id == pod_id), None)
        
        if pod and pod.status.upper() == "RUNNING" and pod.ssh_cmd:
            return pod
        
        time.sleep(10)  # Check every 10 seconds


def get_pytorch_template_id() -> Optional[str]:
    """Get the template ID for the newest PyTorch template."""
    
    lium = Lium()
    templates = lium.templates()

    if config.default_template_id in {t.id for t in templates}:
        return config.default_template_id
    
    # Filter PyTorch templates from daturaai/pytorch
    pytorch_templates = [
        t for t in templates 
        if t.category.upper() == "PYTORCH" 
        and t.docker_image == "daturaai/pytorch"
        and t.name.startswith("Pytorch (Cuda)")
    ]
    
    if not pytorch_templates:
        return None
    
    # Sort by PyTorch version (extract version from tag)
    def extract_pytorch_version(template):
        tag = template.docker_image_tag
        # Extract version like "2.6.0" from "2.6.0-py3.11-cuda12.5.1-devel-ubuntu24.04"
        version_part = tag.split('-')[0]
        try:
            # Split version into major.minor.patch for proper sorting
            parts = [int(x) for x in version_part.split('.')]
            return tuple(parts)
        except:
            return (0, 0, 0)

    # Get the template with highest version
    newest_template = max(pytorch_templates, key=extract_pytorch_version)
    return newest_template.id


def ensure_config():
    from .init.actions import SetupApiKeyAction, SetupSshKeyAction
    from lium.cli.settings import config

    if not config.get('api.api_key'):
        # Setup API key
        action = SetupApiKeyAction()
        action.execute({})

    if not config.get('ssh.key_path'):
        # Setup SSH key
        action = SetupSshKeyAction()
        action.execute({})


def ensure_backup_params(
    enabled: bool = True, 
    path: str = config.default_backup_path, 
    frequency: int = config.default_backup_frequency, 
    retention: int = config.default_backup_retention, 
    skip_prompts: bool = False
) -> BackupParams:
    """Create and validate backup parameters, prompt if needed.
    
    - If prompts run: Enter -> default, invalid -> ask again (uniform for all fields).
    - If prompts are skipped: uses passed values as-is.
    """
    if not enabled:
        return BackupParams(enabled=False)

    final_path, final_frequency, final_retention = path, frequency, retention

    # Keep original behavior: only prompt when using the built-in defaults and prompts not skipped
    default_tuple = (config.default_backup_path, config.default_backup_frequency, config.default_backup_retention)
    if not skip_prompts and (path, frequency, retention) == default_tuple:
        console.info("Configuring automated backups...")

        final_path = _prompt_value(
            "[cyan]Backup path[/cyan]",
            default_value=config.default_backup_path,
            value=path,
            cast=str,
            validate=lambda p: isinstance(p, str) and p.startswith("/"),
        )

        final_frequency = _prompt_value(
            "[cyan]Backup frequency in hours[/cyan] (e.g., 6, 12, 24)",
            default_value=config.default_backup_frequency,
            value=frequency,
            cast=int,
            validate=lambda x: isinstance(x, int) and x > 0,
        )

        final_retention = _prompt_value(
            "[cyan]Backup retention in days[/cyan] (e.g., 7, 14, 30)",
            default_value=config.default_backup_retention,
            value=retention,
            cast=int,
            validate=lambda x: isinstance(x, int) and x > 0,
        )

    params = BackupParams(
        enabled=True,
        path=final_path,
        frequency=final_frequency,
        retention=final_retention,
    )
    # Let validate() raise with a clear message if something is wrong
    params.validate()
    return params


def setup_backup(lium, pod: PodInfo, backup_params: BackupParams, replace_existing: bool = True) -> None:
    """Setup backup for a pod using lium SDK.
    
    Args:
        lium: Lium SDK instance
        pod: PodInfo for the target pod
        backup_params: Backup configuration parameters
    """
    if not backup_params.enabled:
        return
    
    pod_name = pod.name or pod.huid
    
    try:
        lium.backup_create(
            pod=pod,
            path=backup_params.path,
            frequency_hours=backup_params.frequency,
            retention_days=backup_params.retention
        )
    except Exception as e:
        if "Backup configuration already exists" in str(e) and replace_existing:
            # remove existing one
            backup_config = lium.backup_config(pod=pod)
            if backup_config:
                lium.backup_delete(backup_config.id)
            # try again
            return setup_backup(lium, pod, backup_params, replace_existing=False)
        elif "API error 400" in str(e):
            data_str = str(e).split("API error 400:")[-1].strip()
            data = json.loads(data_str) if data_str else {}
            if "message" in data:
                console.error(f"Failed to setup backup: {data['message']}")
                return

        console.error(f"Failed to setup backup: {e}")
        raise
