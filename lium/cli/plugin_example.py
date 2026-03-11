"""
Example plugin structure for lium-compose or other Lium CLI plugins.

This file demonstrates how to create a plugin that integrates with Lium CLI.
A real plugin would be in a separate package (e.g., lium-compose).

To create a plugin:

1. Create a new package with this structure:
   lium-compose/
   ├── pyproject.toml
   ├── lium_compose/
   │   ├── __init__.py
   │   ├── commands.py
   │   └── compose.py

2. In pyproject.toml:
   ```toml
   [project]
   name = "lium-compose"
   version = "0.1.0"
   dependencies = ["lium-sdk", "click", "pyyaml"]
   
   [project.entry-points."lium.plugins"]
   compose = "lium_compose:plugin"
   ```

3. In lium_compose/__init__.py:
"""

import click
from typing import Optional
import yaml
from pathlib import Path


# Plugin interface
def get_command():
    """Return the Click command/group for this plugin."""
    return compose_group


# Plugin metadata
plugin = {
    'get_command': get_command,
    'name': 'compose',
    'version': '1.0.0',
    'description': 'Docker-compose style multi-pod management'
}


# Actual plugin implementation
@click.group(name='compose')
@click.pass_context
def compose_group(ctx):
    """Manage multi-pod configurations with YAML files."""
    pass


@compose_group.command('up')
@click.option('-f', '--file', default='compose.yaml', help='Compose file path')
@click.option('-d', '--detach', is_flag=True, help='Run pods in background')
@click.pass_context
def compose_up(ctx, file: str, detach: bool):
    """Start pods from configuration file."""
    compose_file = Path(file)
    if not compose_file.exists():
        click.echo(f"Error: Compose file '{file}' not found", err=True)
        ctx.exit(1)
    
    with open(compose_file) as f:
        config = yaml.safe_load(f)
    
    # Parse models from config
    models = config.get('models', {})
    
    from lium.sdk import Lium
    lium = Lium()
    
    for model_name, model_config in models.items():
        click.echo(f"Starting {model_name}...")
        
        # Find suitable executor
        gpu_type = model_config.get('gpu_type')
        gpu_count = model_config.get('gpu_count', None)
        template_id = model_config.get('template_id')
        
        # Get available executors
        executors = lium.ls(gpu_type=gpu_type, gpu_count=gpu_count)
        if not executors:
            click.echo(f"No executors available for {gpu_type}", err=True)
            continue
        
        # Start pod
        executor = executors[0]
        pod = lium.up(
            executor=executor.id,
            name=model_name,
            gpu_count=gpu_count,
            template=template_id,
        )
        
        click.echo(f"✓ Started {model_name} on {executor.huid}")


@compose_group.command('down')
@click.option('-f', '--file', default='compose.yaml', help='Compose file path')
@click.pass_context
def compose_down(ctx, file: str):
    """Stop all pods from configuration."""
    compose_file = Path(file)
    if not compose_file.exists():
        click.echo(f"Error: Compose file '{file}' not found", err=True)
        ctx.exit(1)
    
    with open(compose_file) as f:
        config = yaml.safe_load(f)
    
    from lium.sdk import Lium
    lium = Lium()
    
    # Stop all pods matching names in config
    models = config.get('models', {})
    pods = lium.ps()
    
    for model_name in models.keys():
        for pod in pods:
            if pod.name == model_name:
                click.echo(f"Stopping {model_name}...")
                lium.down(pod)
                click.echo(f"✓ Stopped {model_name}")


@compose_group.command('ps')
@click.option('-f', '--file', default='compose.yaml', help='Compose file path')
def compose_ps(file: str):
    """List pods from configuration."""
    compose_file = Path(file)
    if not compose_file.exists():
        click.echo(f"Error: Compose file '{file}' not found", err=True)
        return
    
    with open(compose_file) as f:
        config = yaml.safe_load(f)
    
    from lium.sdk import Lium
    lium = Lium()
    
    # List pods matching config
    models = config.get('models', {})
    pods = lium.ps()
    
    click.echo("Configured pods:")
    for model_name in models.keys():
        found = False
        for pod in pods:
            if pod.name == model_name:
                click.echo(f"  {model_name}: {pod.status} ({pod.huid})")
                found = True
                break
        if not found:
            click.echo(f"  {model_name}: not running")
