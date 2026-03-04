"""Tests for the Task data model."""
import time

from kernos.kernel.task import (
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
    generate_task_id,
)


def test_task_creation_defaults():
    task = Task(
        id="task_123_abcd",
        type=TaskType.REACTIVE_SIMPLE,
        tenant_id="sms:+15555550100",
        conversation_id="+15555550100",
    )
    assert task.status == TaskStatus.PENDING
    assert task.priority == TaskPriority.USER_INTERACTIVE
    assert task.created_at == ""
    assert task.started_at == ""
    assert task.completed_at == ""
    assert task.source == "user_message"
    assert task.input_text == ""
    assert task.result_text == ""
    assert task.error_message == ""
    assert task.input_tokens == 0
    assert task.output_tokens == 0
    assert task.estimated_cost_usd == 0.0
    assert task.duration_ms == 0
    assert task.tool_iterations == 0


def test_task_type_values():
    assert TaskType.REACTIVE_SIMPLE.value == "reactive_simple"


def test_task_status_values():
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.FAILED.value == "failed"


def test_task_priority_constants():
    assert TaskPriority.USER_INTERACTIVE == 1
    assert TaskPriority.PROACTIVE_ALERT == 2
    assert TaskPriority.PROACTIVE_INSIGHT == 5
    assert TaskPriority.BACKGROUND == 8
    # Lower number = higher priority
    assert TaskPriority.USER_INTERACTIVE < TaskPriority.BACKGROUND


def test_generate_task_id_format():
    task_id = generate_task_id()
    assert task_id.startswith("task_")
    parts = task_id.split("_")
    assert len(parts) == 3  # "task", timestamp, rand
    assert parts[2].isalnum()
    assert len(parts[2]) == 4


def test_generate_task_id_sortable():
    id1 = generate_task_id()
    time.sleep(0.001)
    id2 = generate_task_id()
    # Lexicographic order matches chronological order
    assert id1 < id2


def test_generate_task_id_unique():
    ids = {generate_task_id() for _ in range(100)}
    assert len(ids) == 100
