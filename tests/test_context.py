import pytest
from unittest.mock import patch

from orchestrator.context import (
    assemble_api_call,
    format_worker_results,
    load_prompt_file,
    resolve_documents,
)


# ---------------------------------------------------------------------------
# Cache layout — assemble_api_call
# ---------------------------------------------------------------------------

def test_system_prompt_has_cache_control():
    result = assemble_api_call(role="pm", context={"system_prompt": "You are a PM."})
    sys_blocks = result["system"]
    assert len(sys_blocks) == 1
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_blocks[0]["text"] == "You are a PM."


def test_documents_block_has_cache_control():
    with patch("orchestrator.context.resolve_documents", return_value="doc content"):
        result = assemble_api_call(
            role="pm",
            context={"system_prompt": "Prompt.", "documents": ["spec.md"]},
        )
    user_content = result["messages"][0]["content"]
    assert len(user_content) == 2
    doc_block = user_content[0]
    assert doc_block["cache_control"] == {"type": "ephemeral"}
    assert doc_block["text"] == "doc content"


def test_dynamic_block_has_no_cache_control():
    result = assemble_api_call(
        role="pm",
        context={"system_prompt": "Prompt.", "inline": "Do this task."},
    )
    user_content = result["messages"][0]["content"]
    dynamic_block = user_content[-1]
    assert "cache_control" not in dynamic_block
    assert "Do this task." in dynamic_block["text"]


def test_no_documents_produces_single_user_block():
    result = assemble_api_call(
        role="pm",
        context={"system_prompt": "Prompt."},
    )
    user_content = result["messages"][0]["content"]
    assert len(user_content) == 1
    assert "cache_control" not in user_content[0]


def test_empty_inline_uses_begin_sentinel():
    result = assemble_api_call(
        role="pm",
        context={"system_prompt": "Prompt."},
    )
    user_content = result["messages"][0]["content"]
    assert user_content[-1]["text"] == "Begin."


def test_inline_text_preserved():
    result = assemble_api_call(
        role="pm",
        context={"system_prompt": "Prompt.", "inline": "Build the auth module."},
    )
    user_content = result["messages"][0]["content"]
    assert "Build the auth module." in user_content[-1]["text"]


# ---------------------------------------------------------------------------
# Worker results injection
# ---------------------------------------------------------------------------

def test_worker_results_appear_in_dynamic_block():
    worker_results = {"w1": "output from worker one"}
    result = assemble_api_call(
        role="dev_manager",
        context={"system_prompt": "You are a manager.", "worker_results": ["w1"]},
        worker_results=worker_results,
    )
    dynamic_text = result["messages"][0]["content"][-1]["text"]
    assert "output from worker one" in dynamic_text
    assert "w1" in dynamic_text


def test_worker_results_and_inline_combined():
    worker_results = {"w1": "worker output"}
    result = assemble_api_call(
        role="dev_manager",
        context={
            "system_prompt": "Manager.",
            "inline": "Check interface consistency.",
            "worker_results": ["w1"],
        },
        worker_results=worker_results,
    )
    dynamic_text = result["messages"][0]["content"][-1]["text"]
    assert "Check interface consistency." in dynamic_text
    assert "worker output" in dynamic_text


def test_worker_results_block_has_no_cache_control():
    worker_results = {"w1": "some output"}
    result = assemble_api_call(
        role="dev_manager",
        context={"system_prompt": "Manager.", "worker_results": ["w1"]},
        worker_results=worker_results,
    )
    user_content = result["messages"][0]["content"]
    assert "cache_control" not in user_content[-1]


def test_no_worker_results_no_extra_block():
    result = assemble_api_call(
        role="pm",
        context={"system_prompt": "Prompt."},
        worker_results=None,
    )
    user_content = result["messages"][0]["content"]
    assert len(user_content) == 1


# ---------------------------------------------------------------------------
# Missing prompt file error
# ---------------------------------------------------------------------------

def test_missing_prompt_file_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt_file("nonexistent_role_xyz_123")


def test_context_system_prompt_bypasses_file_load():
    # Providing system_prompt in context must not attempt any file I/O
    result = assemble_api_call(
        role="nonexistent_role_xyz_123",
        context={"system_prompt": "Custom prompt content"},
    )
    assert result["system"][0]["text"] == "Custom prompt content"


def test_missing_role_without_system_prompt_raises():
    with pytest.raises(FileNotFoundError):
        assemble_api_call(
            role="nonexistent_role_xyz_123",
            context={},
        )


# ---------------------------------------------------------------------------
# format_worker_results
# ---------------------------------------------------------------------------

def test_format_worker_results_structure():
    formatted = format_worker_results({"w1": "alpha", "w2": "beta"})
    assert "=== Result from w1 ===" in formatted
    assert "alpha" in formatted
    assert "=== Result from w2 ===" in formatted
    assert "beta" in formatted


def test_format_worker_results_empty():
    assert format_worker_results({}) == ""


# ---------------------------------------------------------------------------
# resolve_documents
# ---------------------------------------------------------------------------

def test_resolve_missing_document_returns_placeholder(tmp_path):
    result = resolve_documents(["nonexistent/path/file.md"])
    assert "[File not found]" in result


def test_resolve_existing_document(tmp_path):
    doc = tmp_path / "spec.md"
    doc.write_text("Project spec content")
    result = resolve_documents([str(doc)])
    assert "Project spec content" in result
    assert str(doc) in result
