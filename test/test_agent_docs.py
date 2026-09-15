"""docs/agents.md must describe flags and commands that exist.

Every ``lium …`` invocation on the page (fenced blocks and inline code) is resolved against the click command tree:
the subcommand chain must exist and every ``--flag`` / ``-x`` on the line must be a parameter of that command.
Environment variables the page names as ours (``LIUM_*``) must appear in the source tree. The page cannot drift
from the CLI it documents (review wave 7 Sep 2026: docs described ``exec -d``, ``up --verify-gpus`` and four
commands that did not exist on the branch).
"""

import re
from pathlib import Path

import click
import pytest

from lium.cli.cli import cli

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DOC = ROOT / "docs" / "agents.md"


def _strip_quoted(s: str) -> str:
    return re.sub(r"\"[^\"]*\"|'[^']*'", " ", s)


def _lium_invocations(text: str):
    """(line_no, tokens) for every `lium …` command on the page, cut at shell operators outside quotes."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"<[^<>\n]{1,40}>", "PLACEHOLDER", line)   # `<pod>` is an argument, not a redirect
        for m in re.finditer(r"(?:^|[\s`$>('\"])lium\s+(?=\S)", line):   # `trap 'lium rm …' EXIT` counts too
            rest, cmd, quote = line[m.end():], "", None
            for ch in rest:
                if quote:
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif ch in "|&;>#`":
                    break
                cmd += ch
            toks = _strip_quoted(cmd).split()
            if "--" in toks:
                toks = toks[: toks.index("--")]
            if toks and re.fullmatch(r"[a-z][a-z0-9-]*", toks[0]) and toks[0] != "PLACEHOLDER":
                out.append((no, toks))   # `lium = Lium()` is Python, not an invocation
    return out


def _resolve(tokens):
    """→ (command, chain, flags) or None when the first token is prose (e.g. 'lium requires …')."""
    command, chain, i = cli, [], 0
    while i < len(tokens) and isinstance(command, click.Group) and tokens[i] in command.commands:
        command = command.commands[tokens[i]]
        chain.append(tokens[i])
        i += 1
    if not chain:
        return None
    flags = [re.sub(r"=.*$", "", t) for t in tokens if re.fullmatch(r"--?[A-Za-z][\w-]*(=\S*)?", t)]
    return command, chain, flags


SHELL_FENCES = ("", "bash", "sh", "shell", "console")


def _code_blocks(text: str, fence: str) -> str:
    """The bodies of the page's shell code blocks only (prose lines such as "lium requires" are not commands)."""
    if fence == "md":
        # every fence is paired in order and then filtered by language: an unpaired ```python opener would
        # otherwise make the *prose* after it the next "block" and hide every shell block that follows
        return "\n".join(body for lang, body in re.findall(r"```(\w*)[^\n]*\n(.*?)```", text, re.S) if lang in SHELL_FENCES)
    # rst: `.. code-block:: bash` bodies are the indented lines that follow
    blocks, out = re.split(r"^\.\. code-block:: (?:bash|sh|shell|console)\s*$", text, flags=re.M)[1:], []
    for block in blocks:
        body = []
        for line in block.splitlines()[1:]:
            if line.strip() == "" and not body:
                continue
            if line and not line.startswith((" ", "\t")):
                break
            body.append(line.strip())
        out.append("\n".join(body))
    return "\n".join(out)


# every `lium …` line the three guides show a reader: agents.md whole, README and getting-started's shell blocks
PAGES = {
    "docs/agents.md": AGENTS_DOC.read_text(),
    "README.md": _code_blocks((ROOT / "README.md").read_text(), "md"),
    "docs/getting-started.rst": _code_blocks((ROOT / "docs" / "getting-started.rst").read_text(), "rst"),
}
INVOCATIONS = [(page, no, toks) for page, text in PAGES.items() for no, toks in _lium_invocations(text)]


def test_the_pages_have_commands_to_check():
    """Regression guard for the parametrized test below: if `_lium_invocations` or `_code_blocks` breaks (a fence
    regex change, a new README layout) the invocation list shrinks towards zero and every documented line
    "resolves" because none is checked. The floors are well under today's counts (agents 40+, README 70+)."""
    per_page = {page: sum(1 for p, _, _ in INVOCATIONS if p == page) for page in PAGES}
    assert per_page["docs/agents.md"] >= 30 and per_page["README.md"] >= 60 and per_page["docs/getting-started.rst"] >= 1, per_page


def test_shell_blocks_after_a_python_block_are_still_read():
    """`_code_blocks` pairs fences by language: a ```python block does not swallow the shell blocks after it, and
    neither prose between blocks nor the python itself is treated as a command line."""
    page = (
        "# Title\n\n```bash\nlium ls\n```\n\nProse: lium requires a key.\n\n"
        "```python\nlium = Lium()\nlium.up()\n```\n\nMore prose with lium in it.\n\n"
        "```bash\nlium ps --format json\n```\n\n```\nlium rm 1 --yes\n```\n"
    )
    blocks = _code_blocks(page, "md")
    assert [line for line in blocks.splitlines() if line] == ["lium ls", "lium ps --format json", "lium rm 1 --yes"]
    assert [toks for _, toks in _lium_invocations(blocks)] == [["ls"], ["ps", "--format", "json"], ["rm", "1", "--yes"]]
    # the README's CLI Reference block comes after two ```python blocks: python assignments and prose never read as commands
    readme = [toks for page, _, toks in INVOCATIONS if page == "README.md"]
    assert not any(toks[0] == "=" or toks[0].startswith("requires") for toks in readme)


def test_a_quoted_lium_line_is_resolved_too():
    trap = "trap 'lium rm \"$POD\" --yes >/dev/null 2>&1 || true' EXIT"
    assert [toks for _, toks in _lium_invocations(trap)] == [["rm", "--yes"]]


@pytest.mark.parametrize("page, line_no, tokens", INVOCATIONS, ids=[f"{p.split('/')[-1]} L{n}: lium {' '.join(t[:3])}" for p, n, t in INVOCATIONS])
def test_every_documented_lium_line_resolves(page, line_no, tokens):
    resolved = _resolve(tokens)
    assert resolved is not None, f"{page}:{line_no}: `lium {tokens[0]}` is not a command"
    command, chain, flags = resolved
    known = {opt for param in command.params for opt in param.opts} | {"--help"}
    passthrough = command.context_settings.get("ignore_unknown_options") or command.context_settings.get("allow_extra_args")
    for flag in flags:
        if flag in ("-", "--") or (flag.startswith("-") and not flag.startswith("--") and len(flag) != 2):
            continue
        assert passthrough or flag in known, f"{page}:{line_no}: `lium {' '.join(chain)} {flag}` — no such option"


def test_env_vars_named_as_ours_exist():
    text = AGENTS_DOC.read_text()
    source = "\n".join(p.read_text() for p in (ROOT / "lium").rglob("*.py"))
    for var in sorted(set(re.findall(r"\bLIUM_[A-Z0-9_]+\b", text))):
        assert var in source, f"docs/agents.md names `{var}` but nothing in lium/ reads it"


def test_the_json_flag_command_list_matches_the_cli():
    """§2 names the commands that take `--json` in prose, not as `lium …` lines, so the invocation test above
    cannot see it (the list once said `rm` and `up`, neither of which has the flag)."""
    text = AGENTS_DOC.read_text()
    m = re.search(r"command that takes `--json` \(([^)]*)\)", text)
    assert m, "docs/agents.md §2 no longer says which commands take --json"
    named = re.findall(r"`([^`]+)`", m.group(1))
    assert named, m.group(0)
    for name in named:
        resolved = _resolve(name.split())
        assert resolved and len(resolved[1]) == len(name.split()), f"`lium {name}` is not a command"
        assert "--json" in {opt for param in resolved[0].params for opt in param.opts}, f"`lium {name}` has no --json"
    # and the other way round: every renter command with --json is on the list (provider/mine are not renter commands);
    # a hidden option (an alias `--help` does not show, e.g. lium#217's `--json` on ls/ps) is not what the page documents

    def with_json(group, chain):
        for sub, command in sorted(group.commands.items()):
            if sub in ("provider", "mine") or getattr(command, "hidden", False):
                continue
            if isinstance(command, click.Group):
                yield from with_json(command, chain + [sub])
            elif "--json" in {opt for param in command.params if not getattr(param, "hidden", False) for opt in param.opts}:
                yield " ".join(chain + [sub])
    assert sorted(named) == sorted(with_json(cli, [])), "agents.md §2 and the command tree disagree on which commands take --json"


CAPTURED_EXEC = re.compile(r"\$\(\s*lium\s+exec\s+(?P<line>[^\n]*)")


def _captured_exec_lines(text: str):
    """Every `$(lium exec …)` on a page: (line no, the text from `lium exec` on, the invocation's own tokens).

    The tokens come from `_lium_invocations`, cut at the first `|`, `)`, `;` … outside quotes, so a `--json` in the
    remote command string or on a later command of the same line does not count."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        for m in CAPTURED_EXEC.finditer(line):
            rest = "lium exec " + m.group("line")
            tokens = next((toks for _, toks in _lium_invocations(rest) if toks[:1] == ["exec"]), [])
            out.append((no, m.group("line"), tokens))
    return out


def test_a_captured_exec_goes_through_json_and_jq():
    """`lium exec <pod> "cmd"` prints `Executing on <pod>` on stdout before the remote output (exec.py, the
    non-`--json` branch), so `PID=$(lium exec …)` without `--json` is two lines, not a PID — 669f554 shipped
    exactly that. A capture on the page reads the envelope: `--json … | jq -r '.results[0].stdout'`."""
    captures = [(page, no, line, toks) for page, text in PAGES.items() for no, line, toks in _captured_exec_lines(text)]
    assert captures, "docs/agents.md no longer shows a captured `$(lium exec …)` (the background-job recipe)"
    for page, no, line, toks in captures:
        assert "--json" in toks, f"{page}:{no}: `$(lium exec …)` without --json captures `Executing on <pod>` too"
    pid_lines = [line for _, _, line, _ in captures if "echo \\$!" in line]
    assert pid_lines, "docs/agents.md no longer shows the `… & echo \\$!` PID capture"
    for line in pid_lines:
        assert "jq -r '.results[0].stdout'" in line, f"a PID capture reads the envelope's `.results[0].stdout`: {line}"


def test_the_capture_check_fails_the_plain_form():
    plain = 'PID=$(lium exec "$POD" "nohup setsid bash -lc train.sh > /root/logs/t.log 2>&1 < /dev/null & echo \\$!")'
    (no, _, toks), = _captured_exec_lines(plain)
    assert no == 1 and "--json" not in toks
    # a --json elsewhere on the line, or inside the remote command string, does not make the capture a JSON one
    elsewhere = 'PID=$(lium exec "$POD" "echo --json & echo \\$!" | tr -d "[:space:]"); lium balance --json'
    (_, _, toks), = _captured_exec_lines(elsewhere)
    assert "--json" not in toks


def test_the_documented_capture_reads_the_pid_from_the_envelope_the_cli_emits():
    """The jq path on the page against what `report_executions(json_output=True)` writes: the remote stdout alone."""
    import json
    import shutil
    import subprocess

    from click.testing import CliRunner

    from lium.cli.commands.exec import PodExecution, report_executions

    (line,) = [captured for _, text in PAGES.items() for _, captured, _ in _captured_exec_lines(text) if "echo \\$!" in captured]
    jq_filter = re.search(r"jq -r '([^']+)'", line).group(1)
    execution = PodExecution(pod="swift-fox-c8", stdout="48213\n", stderr="", exit_code=0, error=None)
    envelope = CliRunner().invoke(click.command()(lambda: report_executions([execution], json_output=True)), []).output
    assert json.loads(envelope)["results"][0]["stdout"] == "48213\n"
    if shutil.which("jq") is None:
        pytest.skip("jq not installed here; the envelope path was checked with json.loads")
    pid = subprocess.run(["jq", "-r", jq_filter], input=envelope, capture_output=True, text=True, check=True).stdout.strip()
    assert pid == "48213"


def test_the_documented_failure_branch_reads_the_remote_failure_from_stdout():
    """A remote non-zero exit prints the `results[]` envelope on stdout and exits with the remote code; stderr
    (the page's `err.json`) stays empty (exec.py `report_executions` + `SystemExit`). The page's script pattern
    reads `$out` first; its jq filter names the pod, the exit code and the remote stderr (arhangel66 on #213)."""
    import json
    import shutil
    import subprocess

    from click.testing import CliRunner

    from lium.cli.commands.exec import PodExecution, report_executions

    text = PAGES["docs/agents.md"]
    assert 'if [ -n "$out" ]; then' in text, "the failure pattern no longer checks stdout before err.json"
    jq_filter = re.search(r"jq -r '(\.results\[\] \| [^']+)'", text).group(1)
    execution = PodExecution(pod="train-1", stdout="", stderr="nvidia-smi: not found\n", exit_code=127, error=None)
    result = CliRunner().invoke(click.command()(lambda: report_executions([execution], json_output=True)), [])
    envelope = result.stdout
    assert json.loads(envelope)["results"][0]["exit_code"] == 127 and result.stderr == ""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed here; the envelope was checked with json.loads")
    line = subprocess.run(["jq", "-r", jq_filter], input=envelope, capture_output=True, text=True, check=True).stdout
    assert line.startswith("train-1: exit 127: nvidia-smi: not found")
