from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _cfg(**overrides):
    cfg = {
        "enabled": True,
        "provider": {
            "max_calls_per_hour": 4,
            "max_calls_per_day": 10,
            "max_tokens_per_hour": 10_000,
            "max_tokens_per_day": 20_000,
        },
        "session": {
            "max_calls": 3,
            "max_total_tokens": 5_000,
            "repeated_request_limit": 2,
        },
        "background": {
            "max_calls_per_hour": 2,
            "max_calls_per_day": 4,
            "max_tokens_per_hour": 4_000,
            "max_tokens_per_day": 8_000,
        },
        "quota": {
            "background_block_percent": 80,
            "foreground_fallback_percent": 95,
            "poll_seconds": 60,
        },
        "alerts": {"cooldown_seconds": 3600, "operator_target": ""},
    }
    cfg.update(overrides)
    return cfg


def _response(input_tokens=10, output_tokens=5, model="m"):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_background_quota_signal_blocks_before_xai_exhaustion(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    quota = {"used_percent": 81.0, "reset_at": "2026-09-24T17:04:00Z", "source": "xai_live"}
    provider_called = False

    def provider_call(_request):
        nonlocal provider_called
        provider_called = True
        return _response()

    with pytest.raises(BudgetExceeded) as exc:
        guarded_model_call(
            {"messages": [{"role": "user", "content": "small"}]},
            provider_call,
            provider="xai-oauth",
            model="grok-4.6",
            session_id="cron-1",
            platform="cron",
            role="primary",
            task="daily",
            config=_cfg(),
            state_dir=tmp_path,
            quota_snapshot=quota,
        )

    assert exc.value.code == "provider_quota_background"
    assert exc.value.fallback_recommended is False
    assert "81.0%" in str(exc.value)
    assert provider_called is False


def test_foreground_quota_signal_requests_controlled_fallback(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    quota = {"used_percent": 96.0, "reset_at": "2026-09-24T17:04:00Z", "source": "xai_live"}
    provider_called = False

    def provider_call(_request):
        nonlocal provider_called
        provider_called = True
        return _response()

    with pytest.raises(BudgetExceeded) as exc:
        guarded_model_call(
            {"messages": [{"role": "user", "content": "small"}]},
            provider_call,
            provider="xai-oauth",
            model="grok-4.6",
            session_id="chat-1",
            platform="telegram",
            role="primary",
            config=_cfg(),
            state_dir=tmp_path,
            quota_snapshot=quota,
        )

    assert exc.value.code == "provider_quota_foreground"
    assert exc.value.fallback_recommended is True
    assert provider_called is False


def test_hard_session_call_budget_stops_runaway_loop(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    request = {"messages": [{"role": "user", "content": "one"}]}
    cfg = _cfg()
    cfg["session"]["repeated_request_limit"] = 10
    for n in range(3):
        guarded_model_call(
            request,
            lambda _r, n=n: _response(input_tokens=100 + n, output_tokens=10),
            provider="openai-codex",
            model="gpt-5.6-sol",
            session_id="same-session",
            platform="telegram",
            role="primary",
            config=cfg,
            state_dir=tmp_path,
        )

    with pytest.raises(BudgetExceeded) as exc:
        guarded_model_call(
            request,
            lambda _r: _response(),
            provider="openai-codex",
            model="gpt-5.6-sol",
            session_id="same-session",
            platform="telegram",
            role="primary",
            config=cfg,
            state_dir=tmp_path,
        )
    assert exc.value.code == "session_calls"
    assert exc.value.fallback_recommended is False
    assert "new session" in str(exc.value).lower()


def test_provider_token_budget_is_shared_across_sessions(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    cfg = _cfg()
    cfg["provider"]["max_tokens_per_hour"] = 140
    cfg["provider"]["max_tokens_per_day"] = 140
    request = {"messages": [{"role": "user", "content": "one"}], "max_tokens": 1}
    guarded_model_call(
        request,
        lambda _r: _response(input_tokens=100, output_tokens=40),
        provider="p",
        model="m",
        session_id="s1",
        platform="telegram",
        role="primary",
        config=cfg,
        state_dir=tmp_path,
    )
    with pytest.raises(BudgetExceeded) as exc:
        guarded_model_call(
            request,
            lambda _r: _response(input_tokens=1, output_tokens=1),
            provider="p",
            model="m",
            session_id="s2",
            platform="telegram",
            role="primary",
            config=cfg,
            state_dir=tmp_path,
        )
    assert exc.value.code == "provider_tokens_hour"


def test_auxiliary_role_uses_background_budget_even_on_foreground_platform(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    cfg = _cfg()
    cfg["background"]["max_calls_per_hour"] = 1
    request = {"messages": [{"role": "user", "content": "title"}]}
    guarded_model_call(
        request,
        lambda _r: _response(),
        provider="openai-codex",
        model="gpt-5.4-mini",
        session_id="s",
        platform="telegram",
        role="auxiliary:title_generation",
        config=cfg,
        state_dir=tmp_path,
    )
    with pytest.raises(BudgetExceeded) as exc:
        guarded_model_call(
            request,
            lambda _r: _response(),
            provider="openai-codex",
            model="gpt-5.4-mini",
            session_id="s2",
            platform="telegram",
            role="auxiliary:compression",
            config=cfg,
            state_dir=tmp_path,
        )
    assert exc.value.code == "background_calls_hour"


def test_threshold_and_block_alerts_are_deduplicated(tmp_path):
    from agent.model_spend_governor import BudgetExceeded, guarded_model_call

    cfg = _cfg()
    cfg["session"]["max_calls"] = 1
    request = {"messages": [{"role": "user", "content": "x"}]}
    guarded_model_call(
        request,
        lambda _r: _response(),
        provider="p",
        model="m",
        session_id="s",
        platform="telegram",
        role="primary",
        config=cfg,
        state_dir=tmp_path,
    )
    for _ in range(2):
        with pytest.raises(BudgetExceeded):
            guarded_model_call(
                request,
                lambda _r: _response(),
                provider="p",
                model="m",
                session_id="s",
                platform="telegram",
                role="primary",
                config=cfg,
                state_dir=tmp_path,
            )

    rows = [json.loads(line) for line in (tmp_path / "operator_alerts.jsonl").read_text().splitlines()]
    block_rows = [r for r in rows if r["event"] == "block" and r["code"] == "session_calls"]
    assert len(block_rows) == 1


def test_explicit_model_forbidden_environment_fails_closed(tmp_path, monkeypatch):
    from agent.model_spend_governor import BudgetExceeded, preflight_request

    monkeypatch.setenv("HERMES_MODEL_CALLS_FORBIDDEN", "1")
    with pytest.raises(BudgetExceeded) as exc:
        preflight_request(
            {"messages": [{"role": "user", "content": "x"}]},
            provider="p",
            model="m",
            session_id="s",
            platform="cron",
            role="primary",
            config=_cfg(),
            state_dir=tmp_path,
        )
    assert exc.value.code == "model_calls_forbidden"


def test_stale_background_reservation_does_not_permanently_block(tmp_path):
    from agent import model_spend_governor

    cfg = _cfg(reservation_stale_seconds=10)
    cfg["background"]["max_concurrent"] = 1
    request = {"messages": [{"role": "user", "content": "background"}]}
    first = model_spend_governor.preflight_request(
        request, provider="openai-codex", model="gpt-5.6-sol", session_id="old",
        platform="cron", role="primary", config=cfg, state_dir=tmp_path,
    )
    with model_spend_governor._connect(tmp_path) as conn:
        conn.execute(
            "UPDATE requests SET ts=? WHERE request_id=?",
            (model_spend_governor.time.time() - 11, first.request_id),
        )

    second = model_spend_governor.preflight_request(
        request, provider="openai-codex", model="gpt-5.6-sol", session_id="new",
        platform="cron", role="primary", config=cfg, state_dir=tmp_path,
    )

    assert second.background is True
    with model_spend_governor._connect(tmp_path) as conn:
        assert conn.execute(
            "SELECT status FROM requests WHERE request_id=?", (first.request_id,)
        ).fetchone()[0] == "abandoned"


def test_named_profile_reads_shared_root_governor_policy(tmp_path, monkeypatch):
    import builtins

    import hermes_yaml
    from agent.model_spend_governor import governor_config

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "comms"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(
        hermes_yaml.safe_dump({"spend_governor": {"provider": {"max_calls_per_day": 9}}}),
        encoding="utf-8",
    )
    (profile / "config.yaml").write_text(
        hermes_yaml.safe_dump({"model": {"provider": "openai-codex"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))

    real_import = builtins.__import__

    def import_without_pyyaml(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pyyaml)

    assert governor_config()["provider"]["max_calls_per_day"] == 9


def test_flat_legacy_settings_merge_into_effective_runtime_config():
    from agent.model_spend_governor import governor_config

    effective = governor_config({
        "max_concurrent_background_model_jobs": 2,
        "max_model_launches_per_hour": 3,
        "max_model_launches_per_day": 7,
    })

    assert effective["background"]["max_concurrent"] == 2
    assert effective["background"]["max_calls_per_hour"] == 3
    assert effective["background"]["max_calls_per_day"] == 7
