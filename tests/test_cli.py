"""CLI smoke tests: spawn/list/chat/forget wiring. No real agents run."""

from __future__ import annotations

from quarterdeck.cli import main


def run_cli(tmp_path, *argv):
    return main(["--home", str(tmp_path / "qd"), *argv])


def test_spawn_list_chat_forget(tmp_path, capsys):
    assert run_cli(tmp_path, "spawn", "--name", "scout",
                   "--system", "watch the horizon") == 0
    assert run_cli(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "scout" in out and "idle" in out

    assert run_cli(tmp_path, "chat", "--say", "hello agents") == 0
    assert run_cli(tmp_path, "chat", "--channel", "#general") == 0
    out = capsys.readouterr().out
    assert "hello agents" in out and "<you>" in out

    assert run_cli(tmp_path, "spawn", "--name", "scout",
                   "--system", "dup") == 1  # duplicate -> error, no traceback

    assert run_cli(tmp_path, "forget", "--name", "scout") == 0
    assert run_cli(tmp_path, "forget", "--name", "scout") == 1


def test_spawn_scheduled_needs_prompt(tmp_path):
    assert run_cli(tmp_path, "spawn", "--name", "s", "--system", "x",
                   "--interval", "60") == 2
    assert run_cli(tmp_path, "spawn", "--name", "s", "--system", "x",
                   "--interval", "60", "--prompt", "check in") == 0
