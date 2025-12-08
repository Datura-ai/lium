"""Port forward command implementation."""

import socket
import threading
from typing import Optional
import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import handle_errors, parse_targets


def get_port_mapping(pod: PodInfo, internal_port: int) -> Optional[int]:
    """Get the external port for an internal port."""
    if not pod.ports:
        return None
    return pod.ports.get(str(internal_port))


def forward_connection(client_sock: socket.socket, remote_host: str, remote_port: int):
    """Forward data between client and remote."""
    try:
        remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote_sock.connect((remote_host, remote_port))

        def forward(src, dst):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except:
                pass
            finally:
                try:
                    src.close()
                    dst.close()
                except:
                    pass

        t1 = threading.Thread(target=forward, args=(client_sock, remote_sock), daemon=True)
        t2 = threading.Thread(target=forward, args=(remote_sock, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:
        ui.dim(f"Connection error: {e}")
        client_sock.close()


@click.command("port-forward")
@click.argument("target")
@click.argument("port", type=int)
@click.option("--local-port", "-l", type=int, help="Local port to bind (defaults to same as internal port)")
@handle_errors
def port_forward_command(target: str, port: int, local_port: Optional[int]):
    """Forward a local port to a pod's internal port.

    \b
    TARGET: Pod identifier - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    PORT: The internal port on the pod to forward to.

    \b
    Examples:
      lium port-forward 1 8000              # Forward localhost:8000 -> pod's port 8000
      lium port-forward eager-wolf-aa 8080  # Forward localhost:8080 -> pod's port 8080
      lium port-forward 1 8000 -l 3000      # Forward localhost:3000 -> pod's port 8000
    """
    if local_port is None:
        local_port = port

    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        ui.warning("No active pods")
        return

    pods = parse_targets(target, all_pods)
    pod = pods[0] if pods else None

    if not pod:
        ui.error(f"Pod '{target}' not found")
        return

    if pod.status.lower() != "running":
        ui.error(f"Pod '{pod.huid}' is not running (status: {pod.status})")
        return

    external_port = get_port_mapping(pod, port)
    if not external_port:
        available_ports = list(pod.ports.keys()) if pod.ports else []
        ui.error(f"Port {port} is not exposed on pod '{pod.huid}'")
        if available_ports:
            ui.dim(f"Available internal ports: {', '.join(available_ports)}")
        return

    host = pod.executor.ip if pod.executor else pod.host
    if not host:
        ui.error(f"Cannot determine host IP for pod '{pod.huid}'")
        return

    ui.success(f"Forwarding localhost:{local_port} -> {host}:{external_port} (pod port {port})")
    ui.dim("Press Ctrl+C to stop")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind(("127.0.0.1", local_port))
        server.listen(5)

        while True:
            client_sock, addr = server.accept()
            thread = threading.Thread(
                target=forward_connection,
                args=(client_sock, host, external_port),
                daemon=True
            )
            thread.start()

    except KeyboardInterrupt:
        ui.dim("\nPort forwarding stopped")
    except OSError as e:
        ui.error(f"Failed to bind to port {local_port}: {e}")
    finally:
        server.close()
