"""Tool calls and results in the thought stream."""
import tempfile
from pathlib import Path

from quarterdeck.agent_stream import (
    _format_tool_call,
    _format_tool_result,
    thought_stream_log,
)
from quarterdeck.chat import Chat
from quarterdeck.thoughts import thought_channel


def test_format_tool_call():
    out = _format_tool_call(
        "[pinnace] tool: quarterdeck_send({'channel': '#general', 'text': 'hi'})"
    )
    assert out.startswith("\U0001f527 ")
    assert "quarterdeck_send" in out
    assert "#general" in out


def test_format_tool_call_truncates_long_args():
    out = _format_tool_call("[pinnace] tool: t({'" + "x" * 500 + "'})")
    assert len(out) < 400
    assert "\u2026" in out


def test_format_tool_call_no_parens():
    out = _format_tool_call("[pinnace] tool: sometool")
    assert out.startswith("\U0001f527 sometool")


def test_format_tool_result():
    out = _format_tool_result(
        "[pinnace] tool-result: quarterdeck_send -> posted ok"
    )
    assert out.startswith("\u2192 ")
    assert "posted ok" in out


def test_format_tool_result_truncates():
    out = _format_tool_result("[pinnace] tool-result: t -> " + "y" * 600)
    assert len(out) < 560
    assert "\u2026" in out


def test_format_tool_result_error():
    out = _format_tool_result("[pinnace] tool-result: t -> error: boom")
    assert "error: boom" in out


def test_log_wrapper_posts_all_three_kinds():
    home = Path(tempfile.mkdtemp())
    log = thought_stream_log("tcagent", home=home)
    log("[pinnace] thinking: let me think")
    log("[pinnace] tool: quarterdeck_read({'channel': '#general'})")
    log("[pinnace] tool-result: quarterdeck_read -> 3 msgs")
    log("[pinnace] turn 1/25, calling model...")  # ignored

    msgs = Chat(home=home).history(thought_channel("tcagent"))
    texts = [m["text"] for m in msgs]
    assert len(texts) == 3
    assert texts[0] == "let me think"
    assert texts[1].startswith("\U0001f527 quarterdeck_read")
    assert texts[2].startswith("\u2192 3 msgs")


def test_log_wrapper_passes_through_base():
    home = Path(tempfile.mkdtemp())
    seen = []
    log = thought_stream_log("tcagent2", base_log=seen.append, home=home)
    log("[pinnace] anything at all")
    assert seen == ["[pinnace] anything at all"]
