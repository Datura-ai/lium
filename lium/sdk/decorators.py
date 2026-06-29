"""Higher-level decorators built on top of the Lium SDK."""

import inspect
import json
import os
import random
import shlex
import tempfile
import time
from functools import wraps
from typing import Optional, Sequence

from .client import Lium
from .exceptions import LiumError


def machine(
    machine: str,
    template_id: Optional[str] = None,
    cleanup: bool = True,
    requirements: Optional[Sequence[str]] = None,
):
    """Decorator to execute a function on a remote Lium machine.

    Creates a new pod, sends function source code and executes it remotely,
    returns the result, and optionally cleans up the pod.

    Args:
        machine: Machine type (e.g., "1xH200", "1xA100")
        template_id: Docker template ID (optional, uses default if not specified)
        cleanup: Whether to delete the pod after execution (default: True)
        requirements: Optional iterable of pip-installable packages to install on the pod
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Initialize SDK
            sdk = Lium()
            pod_info = None

            try:
                # Step 1: Find executor matching machine type
                # NOTE (DAH-2254): @machine intentionally calls sdk.ls() with no gpu_count
                # kwarg and filters post-fetch via substring match on executor.machine_name
                # (see line below). The widen_for_splitting kwarg therefore never fires
                # from this path — @machine retains strict "exact machine_name substring"
                # semantics regardless of CLI widen behavior. If you ever add gpu_count=...
                # to this call, decide explicitly whether widen_for_splitting should be True.
                executors = sdk.ls()
                matching_executor = None
                for executor in executors:
                    if machine.lower() in executor.machine_name.lower():
                        matching_executor = executor
                        break

                if not matching_executor:
                    raise LiumError(f"No node found matching machine type: {machine}")

                # Step 2: Create pod
                pod_name = f"remote-{func.__name__}-{int(time.time())}"

                template = sdk.default_docker_template(matching_executor.id)

                pod_dict = sdk.up(
                    executor_id=matching_executor.id,
                    name=pod_name,
                    template_id=template.id if template else None,
                )

                # Wait for pod to be ready
                max_wait = 300
                pod_info = sdk.wait_ready(pod_dict, timeout=max_wait)
                if not pod_info:
                    raise LiumError(f"Pod {pod_name} failed to start within {max_wait}s")

                # Step 3: Extract function source code without decorators
                func_source = inspect.getsource(func)
                func_name = func.__name__

                # Strip decorator lines - find the 'def' line and keep from there
                lines = func_source.split('\n')
                def_index = next(i for i, line in enumerate(lines) if 'def ' in line)
                func_source = '\n'.join(lines[def_index:])

                # Step 4: Create runner script with function source and arguments
                runner_script = f'''#!/usr/bin/env python3
import sys
import traceback
import json

# Function source code
{func_source}

try:
    # Arguments
    args = {repr(args)}
    kwargs = {repr(kwargs)}

    # Execute function
    result = {func_name}(*args, **kwargs)

    # Save result as JSON
    with open('/tmp/result.json', 'w') as f:
        json.dump({{'success': True, 'result': result}}, f)

except Exception as e:
    # Save error
    with open('/tmp/result.json', 'w') as f:
        json.dump({{
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }}, f)
    sys.exit(1)
'''

                # Write runner script to temp file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                    runner_file = f.name
                    f.write(runner_script)

                try:
                    # Step 5: Upload runner script
                    sdk.upload(pod_info, local=runner_file, remote='/tmp/runner.py')

                    # Step 6: Create isolated virtual environment
                    venv_path = f"/tmp/lium_venv_{int(time.time())}_{random.randint(1000,9999)}"
                    venv_python = f"{venv_path}/bin/python"
                    venv_cmd = f"python3 -m venv {shlex.quote(venv_path)}"
                    venv_result = sdk.exec(pod_info, command=venv_cmd)
                    if not venv_result['success']:
                        raise LiumError(f"Failed to create virtual environment:\n{venv_result['stderr']}")

                    # Step 7: Install requirements if requested
                    reqs = [req for req in (requirements or []) if req]
                    if reqs:
                        packages = " ".join(shlex.quote(req) for req in reqs)
                        install_cmd = f"{shlex.quote(venv_python)} -m pip install {packages}"
                        install_result = sdk.exec(pod_info, command=install_cmd)
                        if not install_result['success']:
                            raise LiumError(
                                "Failed installing requirements "
                                f"({', '.join(reqs)}):\n{install_result['stderr']}"
                            )

                    # Step 8: Execute runner via virtual environment python
                    exec_result = sdk.exec(
                        pod_info,
                        command=f"{shlex.quote(venv_python)} /tmp/runner.py"
                    )

                    # Step 9: Download result (even when execution failed to capture error details)
                    result_data = None
                    result_file = None
                    try:
                        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                            result_file = f.name
                        sdk.download(pod_info, remote='/tmp/result.json', local=result_file)
                        with open(result_file, 'r') as f:
                            result_data = json.load(f)
                    except Exception:
                        result_data = None
                    finally:
                        if result_file and os.path.exists(result_file):
                            os.unlink(result_file)

                    if result_data and result_data.get('success'):
                        return result_data['result']

                    # Construct detailed error message
                    if result_data and not result_data.get('success', True):
                        err_msg = result_data.get('error', 'Unknown remote error')
                        tb = result_data.get('traceback')
                        if tb:
                            err_msg = f"{err_msg}\n\nTraceback:\n{tb}"
                        raise LiumError(f"Remote execution failed:\n{err_msg}")

                    stderr = exec_result.get('stderr') or exec_result.get('stdout') or 'Unknown remote error'
                    raise LiumError(f"Remote execution failed:\n{stderr}")

                finally:
                    # Clean up local temp file
                    os.unlink(runner_file)

            finally:
                # Remove virtual environment directory best-effort when pod stays alive
                if pod_info and 'venv_path' in locals():
                    try:
                        sdk.exec(pod_info, command=f"rm -rf {shlex.quote(venv_path)}")
                    except Exception:
                        pass

                # Step 10: Cleanup pod
                if cleanup and pod_info:
                    try:
                        sdk.down(pod_info)
                    except Exception:
                        pass  # Best effort cleanup

        return wrapper

    return decorator


__all__ = ["machine"]
