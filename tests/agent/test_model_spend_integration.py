from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest


def test_main_provider_execution_runs_through_spend_governor(monkeypatch):
    from agent import model_spend_governor, turn_api_call

    captured = {}

    def guarded(request, next_call, **context):
        captured.update(context)
        return next_call(request)

    monkeypatch.setattr(model_spend_governor, "guarded_model_call", guarded)
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _agent: False)
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **_context: next_call(request),
    )
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="openai-codex",
        model="gpt-5.6-sol",
        base_url="",
        platform="telegram",
        session_id="sess",
        is_subagent=False,
        _fallback_index=0,
        _disable_streaming=True,
        _model_request_active=None,
        _pending_redirect_lock=None,
        _pending_redirect=None,
        _has_pending_redirect=lambda: False,
        _interruptible_api_call=lambda request: SimpleNamespace(usage=None, request=request),
        _get_transport=lambda: None,
    )
    verdict = turn_api_call.perform_api_call(
        agent,
        api_kwargs={"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]},
        _original_api_kwargs={},
        _llm_middleware_trace=[],
        _moa_prepared_request=None,
        _retry=SimpleNamespace(),
        thinking_spinner=None,
        retry_count=0,
        api_call_count=1,
        api_request_id="r",
        effective_task_id="t",
        turn_id="turn",
        interrupted=False,
    )
    assert verdict.action == "fallthrough"
    assert captured == {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "session_id": "sess",
        "platform": "telegram",
        "role": "primary",
        "task": "t",
    }


def test_auxiliary_provider_execution_runs_through_same_governor(monkeypatch):
    from agent import auxiliary_client, model_spend_governor

    captured = {}

    def guarded(request, next_call, **context):
        captured.update(context)
        return next_call(request)

    monkeypatch.setattr(model_spend_governor, "guarded_model_call", guarded)
    token = auxiliary_client._RELAY_AUX_CALL_CONTEXT.set({
        "attempt_count": 0,
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "request_id": "r",
        "task": "title_generation",
        "api_mode": "chat_completions",
    })
    try:
        response = auxiliary_client._relay_sync_completion(
            SimpleNamespace(),
            {"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "title"}]},
            provider="openai-codex",
            create=lambda request: SimpleNamespace(usage=None, request=request),
        )
    finally:
        auxiliary_client._RELAY_AUX_CALL_CONTEXT.reset(token)
    assert response is not None
    assert captured["provider"] == "openai-codex"
    assert captured["role"] == "auxiliary:title_generation"
    assert captured["task"] == "title_generation"


def test_auxiliary_async_execution_runs_through_same_governor(monkeypatch):
    from agent import auxiliary_client, model_spend_governor

    captured = {}

    async def guarded(request, next_call, **context):
        captured.update(context)
        return await next_call(request)

    async def create(request):
        return SimpleNamespace(usage=None, request=request)

    monkeypatch.setattr(model_spend_governor, "guarded_model_call_async", guarded)
    token = auxiliary_client._RELAY_AUX_CALL_CONTEXT.set({
        "attempt_count": 0,
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "request_id": "r",
        "task": "compression",
        "api_mode": "chat_completions",
    })
    try:
        response = asyncio.run(auxiliary_client._relay_async_completion(
            SimpleNamespace(),
            {"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "compact"}]},
            provider="openai-codex",
            create=create,
        ))
    finally:
        auxiliary_client._RELAY_AUX_CALL_CONTEXT.reset(token)
    assert response is not None
    assert captured["provider"] == "openai-codex"
    assert captured["role"] == "auxiliary:compression"
    assert captured["task"] == "compression"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            {"id": "pure", "no_agent": True, "spend_class": "pure_script", "model_calls": "forbidden"},
            {"HERMES_CRON_JOB_ID": "pure", "HERMES_MODEL_CALLS_FORBIDDEN": "1"},
        ),
        (
            {"id": "model", "no_agent": True, "spend_class": "model_backed", "model_calls": "governed"},
            {"HERMES_CRON_JOB_ID": "model", "HERMES_MODEL_BACKGROUND": "1"},
        ),
    ],
)
def test_no_agent_cron_environment_contract(job, expected):
    from cron.scheduler_script import _cron_job_environment

    assert _cron_job_environment(job) == expected


def test_unknown_no_agent_cron_classification_fails_closed_before_spawn(monkeypatch):
    from cron import scheduler_script

    spawned = False

    def popen(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("subprocess must not spawn")

    monkeypatch.setattr(scheduler_script, "_resolve_script_path", lambda _path: (SimpleNamespace(), None))
    monkeypatch.setattr(scheduler_script, "_script_argv", lambda _path: (["python", "job.py"], {}, None))
    monkeypatch.setattr(scheduler_script.subprocess, "Popen", popen)

    ok, error = scheduler_script._run_job_script(
        "job.py", job={"id": "unknown", "no_agent": True, "script": "job.py"})

    assert ok is False
    assert "explicit spend_class/model_calls" in error
    assert spawned is False


def test_no_agent_cron_environment_reaches_subprocess(tmp_path, monkeypatch):
    from cron import scheduler_script

    script = tmp_path / "env.py"
    script.write_text(
        "import os; print('|'.join([os.getenv('HERMES_CRON_JOB_ID', ''), "
        "os.getenv('HERMES_MODEL_BACKGROUND', ''), os.getenv('HERMES_MODEL_CALLS_FORBIDDEN', '')]))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_script, "_resolve_script_path", lambda _path: (script, None))

    pure = scheduler_script._run_job_script(
        "env.py", job={"id": "pure", "no_agent": True, "spend_class": "pure_script", "model_calls": "forbidden"})
    model = scheduler_script._run_job_script(
        "env.py", job={"id": "model", "no_agent": True, "spend_class": "model_backed", "model_calls": "governed"})

    assert pure == (True, "pure||1")
    assert model == (True, "model|1|")
    assert "HERMES_MODEL_BACKGROUND" not in os.environ


def _budget_error_kwargs():
    return {
        "thinking_spinner": None,
        "messages": [],
        "api_messages": [{"role": "system", "content": "system"}],
        "api_kwargs": {},
        "system_message": None,
        "active_system_prompt": "system",
        "conversation_history": [],
        "approx_tokens": 10,
        "retry_count": 2,
        "max_retries": 3,
        "compression_attempts": 1,
        "max_compression_attempts": 2,
        "api_call_count": 1,
        "api_request_id": "request",
        "api_start_time": 0.0,
        "effective_task_id": "task",
        "turn_id": "turn",
    }


def test_provider_budget_denial_arms_controlled_fallback(monkeypatch):
    from agent.model_spend_governor import BudgetExceeded
    from agent.turn_api_error import handle_api_error

    notices = []
    agent = SimpleNamespace(
        thinking_callback=None,
        _try_activate_fallback=lambda: True,
        _buffer_diagnostic_status=notices.append,
    )
    monkeypatch.setattr(
        "agent.conversation_loop._arm_fallback_restart",
        lambda _agent, _messages, _prompt, retry: setattr(
            retry, "restart_with_rebuilt_messages", True
        ) or "fallback-system",
    )
    retry = SimpleNamespace(restart_with_rebuilt_messages=False)
    verdict = handle_api_error(
        agent,
        api_error=BudgetExceeded(
            "Provider budget reached.", code="provider_calls_hour", fallback_recommended=True
        ),
        _retry=retry,
        **_budget_error_kwargs(),
    )

    assert verdict.action == "break"
    assert verdict.active_system_prompt == "fallback-system"
    assert verdict.retry_count == 0
    assert verdict.compression_attempts == 0
    assert retry.restart_with_rebuilt_messages is True
    assert "fallback" in notices[0].lower()


def test_session_budget_denial_terminates_without_retry_or_fallback():
    from agent.model_spend_governor import BudgetExceeded
    from agent.turn_api_error import handle_api_error

    fallback_attempted = False

    def activate():
        nonlocal fallback_attempted
        fallback_attempted = True
        return True

    agent = SimpleNamespace(thinking_callback=None, _try_activate_fallback=activate)
    verdict = handle_api_error(
        agent,
        api_error=BudgetExceeded(
            "Model call blocked by hard local session call budget. "
            "Start a new session or compact before retrying.",
            code="session_calls",
        ),
        _retry=SimpleNamespace(),
        **_budget_error_kwargs(),
    )

    assert verdict.action == "return"
    assert verdict.result is not None
    assert verdict.result["failure_retryable"] is False
    assert verdict.result["model_spend_code"] == "session_calls"
    assert "new session or compact" in verdict.result["final_response"].lower()
    assert fallback_attempted is False