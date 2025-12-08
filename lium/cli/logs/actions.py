"""Actions for logs command."""

import json
from typing import Generator

from lium.sdk import Lium, PodInfo


class StreamLogsAction:
    """Stream logs from a pod."""

    def execute(self, ctx: dict) -> Generator[str, None, None]:
        """Stream logs, yielding lines as strings.

        Args:
            ctx: Context dict with 'lium', 'pod', 'tail', 'follow'

        Yields:
            Log lines as strings (parsed from SSE JSON format)
        """
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        tail: int = ctx["tail"]
        follow: bool = ctx["follow"]

        for line in lium.logs(pod.id, tail=tail, follow=follow):
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            # Parse SSE format: "data: {"log": "actual log content"}"
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                    if "log" in data:
                        yield data["log"]
                except json.JSONDecodeError:
                    yield line
            else:
                yield line
