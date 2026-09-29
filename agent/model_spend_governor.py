"""Fail-closed provider/session/background model spend governance.

Every Hermes model request should pass through :func:`guarded_model_call` (or its async
variant).  The governor reserves a request in a cross-profile SQLite ledger before network
I/O, reconciles actual usage afterwards, and enforces provider, session, background, repeated
request, emergency-lock, and live provider-quota gates.  It deliberately owns no billing or
top-up operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar

import hermes_yaml
from hermes_constants import get_hermes_home
from hermes_cli.config_defaults import DEFAULT_CONFIG

logger = logging.getLogger(__name__)
T = TypeVar("T")

_DEFAULTS: dict[str, Any] = DEFAULT_CONFIG["spend_governor"]

_BACKGROUND_PLATFORMS = frozenset({
    "cron", "subagent", "background", "background_review", "curator", "kanban_worker",
})
_XAI_PROVIDERS = frozenset({"xai", "xai-oauth", "grok", "grok-cli"})
_XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


class BudgetExceeded(RuntimeError):
    """A local hard budget or provider quota gate denied a request before network I/O."""

    def __init__(self, message: str, *, code: str, fallback_recommended: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.fallback_recommended = bool(fallback_recommended)
        # Existing provider error plumbing can still render this sensibly when it crosses an
        # integration that has not yet learned the typed exception.
        self.status_code = 429 if fallback_recommended else 403


@dataclass
class RequestLease:
    request_id: str
    state_dir: Path
    provider: str
    model: str
    session_id: str
    background: bool
    reserved_tokens: int

    def complete(self, response: Any = None, *, status: str = "ok") -> None:
        _complete_request(self, response=response, status=status)


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _root_hermes_home(home: Optional[Path] = None) -> Path:
    """Return the shared default-profile root even from ``profiles/<name>``.

    Provider quotas are account-wide; profile-local ledgers would let parallel profiles bypass
    the same budget.
    """
    home = Path(home or get_hermes_home()).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def default_state_dir() -> Path:
    return _root_hermes_home() / "state" / "model_spend"


def _load_yaml_config() -> dict[str, Any]:
    # Provider allowances and the accounting ledger are shared across profiles. Read the same
    # governor policy from the default-profile root so a secondary profile cannot accidentally
    # bypass tighter account-wide limits merely because its isolated config omits this section.
    path = _root_hermes_home() / "config.yaml"
    if not path.exists():
        # Fresh/test profiles inherit registered defaults; only an existing unreadable or
        # malformed config is a fail-closed configuration error.
        return {}
    try:
        # Use Hermes's required YAML runtime rather than the optional PyYAML package. The
        # bundled macOS interpreter intentionally ships ruamel.yaml but not ``yaml``; importing
        # PyYAML here made every model call fail closed even though the same config had already
        # been parsed successfully by the rest of Hermes.
        parsed = hermes_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        raise BudgetExceeded(
            f"Model spend preflight failed closed: cannot parse config.yaml ({exc}).",
            code="config_unreadable",
        ) from exc


def governor_config(config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    raw = dict(config) if config is not None else dict(_load_yaml_config().get("spend_governor") or {})
    merged = _deep_merge(_DEFAULTS, raw)
    # Backward compatibility with the original flat background-launch governor.
    bg = merged["background"]
    aliases = {
        "max_concurrent_background_model_jobs": "max_concurrent",
        "max_model_launches_per_hour": "max_calls_per_hour",
        "max_model_launches_per_day": "max_calls_per_day",
    }
    for old, new in aliases.items():
        if old in raw and new not in (raw.get("background") or {}):
            bg[new] = raw[old]
    if raw.get("operator_alert_target") and not (raw.get("alerts") or {}).get("operator_target"):
        merged["alerts"]["operator_target"] = raw["operator_alert_target"]
    if raw.get("alert_cooldown_seconds") is not None and not (raw.get("alerts") or {}).get("cooldown_seconds"):
        merged["alerts"]["cooldown_seconds"] = raw["alert_cooldown_seconds"]
    return merged


def _int_setting(section: Mapping[str, Any], key: str) -> int:
    try:
        value = int(section[key])
    except Exception as exc:
        raise BudgetExceeded(
            f"Model spend preflight failed closed: invalid spend_governor.{key}.",
            code="invalid_budget_config",
        ) from exc
    if value < 1:
        raise BudgetExceeded(
            f"Model spend preflight failed closed: spend_governor.{key} must be positive.",
            code="invalid_budget_config",
        )
    return value


def _connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "governor.db"), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            session_id TEXT NOT NULL,
            background INTEGER NOT NULL,
            role TEXT NOT NULL,
            task TEXT NOT NULL,
            digest TEXT NOT NULL,
            reserved_tokens INTEGER NOT NULL,
            actual_tokens INTEGER,
            status TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_requests_provider_ts ON requests(provider, ts);
        CREATE INDEX IF NOT EXISTS idx_requests_session_ts ON requests(session_id, ts);
        CREATE INDEX IF NOT EXISTS idx_requests_background_ts ON requests(background, ts);
        CREATE INDEX IF NOT EXISTS idx_requests_digest_ts ON requests(session_id, digest, ts);
        CREATE TABLE IF NOT EXISTS alerts (
            fingerprint TEXT PRIMARY KEY,
            last_sent REAL NOT NULL,
            count INTEGER NOT NULL
        );
        """
    )
    return conn


def _canonical_provider(provider: Optional[str], model: Optional[str]) -> str:
    value = str(provider or "").strip().lower()
    if value:
        return value
    model_value = str(model or "").lower()
    return "xai-oauth" if "grok" in model_value else "unknown"


def _is_background(platform: Optional[str], role: Optional[str]) -> bool:
    role_value = str(role or "").lower()
    return (
        str(platform or "").strip().lower() in _BACKGROUND_PLATFORMS
        or role_value == "delegated"
        or role_value.startswith("auxiliary:")
        or role_value.startswith("background")
        or bool(os.environ.get("HERMES_CRON_JOB_ID"))
    )


def _jsonable_request(request: Mapping[str, Any]) -> bytes:
    selected = {
        "messages": request.get("messages") or request.get("input") or [],
        "tools": request.get("tools") or [],
        "model": request.get("model") or "",
    }
    return json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _request_digest(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(_jsonable_request(request)).hexdigest()


def _estimated_tokens(request: Mapping[str, Any]) -> int:
    payload_chars = len(_jsonable_request(request))
    input_estimate = max(1, (payload_chars + 3) // 4)
    raw_out = request.get("max_completion_tokens", request.get("max_tokens", 256))
    try:
        output_reserve = max(1, min(int(raw_out), 32_000))
    except (TypeError, ValueError):
        output_reserve = 256
    return input_estimate + output_reserve


def _quota_cache_path(state_dir: Path) -> Path:
    return state_dir / "xai_quota.json"


def _read_quota_cache(state_dir: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(_quota_cache_path(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_xai_quota(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_config = payload.get("config")
    config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else payload
    products = config.get("productUsage") or config.get("product_usage") or []
    used = config.get("creditUsagePercent", config.get("credit_usage_percent"))
    product = None
    for row in products if isinstance(products, list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("product") or "").lower() == "grokbuild":
            product = row
            used = row.get("usagePercent", row.get("usage_percent", used))
            break
    period = config.get("currentPeriod") or config.get("current_period") or {}
    try:
        used_percent = float(used)
    except (TypeError, ValueError) as exc:
        raise ValueError("xAI billing response omitted creditUsagePercent") from exc
    return {
        "used_percent": used_percent,
        "reset_at": period.get("end") if isinstance(period, Mapping) else None,
        "period_start": period.get("start") if isinstance(period, Mapping) else None,
        "source": "xai_live",
        "product": (product or {}).get("product") if isinstance(product, Mapping) else "combined",
        "fetched_at": time.time(),
    }


def fetch_xai_quota(*, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Read xAI's billing percentage.  Never purchases, tops up, or mutates billing."""
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    creds = resolve_xai_oauth_runtime_credentials(refresh_if_expiring=True)
    token = str((creds or {}).get("api_key") or "")
    if not token:
        raise RuntimeError("xAI OAuth token unavailable")
    import httpx

    response = httpx.get(
        _XAI_BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-grok-client-surface": "grok-build",
        },
        timeout=timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RuntimeError("xAI billing response was not an object")
    return _extract_xai_quota(payload)


def provider_quota_snapshot(
    provider: str,
    *,
    config: Mapping[str, Any],
    state_dir: Path,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    if _canonical_provider(provider, "") not in _XAI_PROVIDERS:
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    cached = _read_quota_cache(state_dir)
    poll_seconds = float(config["quota"].get("poll_seconds", 60) or 60)
    if cached and not force and time.time() - float(cached.get("fetched_at") or 0) < poll_seconds:
        return cached
    try:
        snapshot = fetch_xai_quota()
        path = _quota_cache_path(state_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return snapshot
    except Exception as exc:
        logger.warning("xAI quota poll failed; hard local budgets remain active: %s", exc)
        # A stale upstream signal remains safer than discarding a known near-limit state. Ignore it
        # only after its reset timestamp has passed.
        if cached:
            reset_at = str(cached.get("reset_at") or "")
            try:
                reset_ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                reset_ts = 0.0
            if not reset_ts or reset_ts > time.time():
                return {**cached, "source": "xai_cached_stale"}
        return None


def _alert_once(
    state_dir: Path,
    config: Mapping[str, Any],
    *,
    event: str,
    code: str,
    provider: str,
    session_id: str,
    message: str,
) -> None:
    now = time.time()
    fingerprint = f"{event}:{code}:{provider}:{session_id if code.startswith('session_') else '*'}"
    cooldown = float(config["alerts"].get("cooldown_seconds", 3600) or 3600)
    should_send = False
    try:
        with _connect(state_dir) as conn:
            row = conn.execute("SELECT last_sent, count FROM alerts WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row is None or now - float(row["last_sent"]) >= cooldown:
                should_send = True
                conn.execute(
                    "INSERT INTO alerts(fingerprint,last_sent,count) VALUES(?,?,1) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET last_sent=excluded.last_sent,count=alerts.count+1",
                    (fingerprint, now),
                )
    except Exception:
        logger.exception("Model spend alert dedupe failed")
        return
    if not should_send:
        return
    row = {
        "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "event": event,
        "code": code,
        "provider": provider,
        "session_id": session_id,
        "message": message,
    }
    try:
        with (state_dir / "operator_alerts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        logger.exception("Model spend operator alert ledger write failed")
    target = str(config["alerts"].get("operator_target") or "").strip()
    if not target or target.startswith("telegram:test"):
        return
    try:
        subprocess.run(
            ["hermes", "send", "--to", target, message[:1500]],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        logger.debug("Model spend operator send failed", exc_info=True)


def _deny(
    state_dir: Path,
    config: Mapping[str, Any],
    *,
    code: str,
    provider: str,
    session_id: str,
    message: str,
    fallback_recommended: bool = False,
) -> None:
    _alert_once(
        state_dir, config, event="block", code=code, provider=provider,
        session_id=session_id, message=message,
    )
    raise BudgetExceeded(message, code=code, fallback_recommended=fallback_recommended)


def _sum(conn: sqlite3.Connection, where: str, args: tuple[Any, ...], *, tokens: bool = False) -> int:
    expression = "COALESCE(SUM(COALESCE(actual_tokens,reserved_tokens)),0)" if tokens else "COUNT(*)"
    return int(conn.execute(f"SELECT {expression} FROM requests WHERE {where}", args).fetchone()[0])


def preflight_request(
    request: Mapping[str, Any],
    *,
    provider: Optional[str],
    model: Optional[str],
    session_id: Optional[str],
    platform: Optional[str],
    role: Optional[str],
    task: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    state_dir: Optional[Path] = None,
    quota_snapshot: Optional[Mapping[str, Any]] = None,
) -> RequestLease:
    cfg = governor_config(config)
    state = Path(state_dir or default_state_dir())
    provider_name = _canonical_provider(provider, model)
    session = str(session_id or "unscoped")
    model_name = str(model or request.get("model") or "unknown")
    background = _is_background(platform, role)

    if os.environ.get("HERMES_MODEL_CALLS_FORBIDDEN", "").strip().lower() in {"1", "true", "yes", "on"}:
        _deny(
            state, cfg, code="model_calls_forbidden", provider=provider_name, session_id=session,
            message="Model call blocked: this pure-script job forbids model subprocesses.",
        )
    root = _root_hermes_home()
    if (root / "EMERGENCY_NO_MODEL").exists():
        _deny(
            state, cfg, code="emergency_lock", provider=provider_name, session_id=session,
            message="Model call blocked by EMERGENCY_NO_MODEL.",
        )
    if not cfg.get("enabled", True):
        return RequestLease(uuid.uuid4().hex, state, provider_name, model_name, session, background, 0)

    quota = dict(quota_snapshot) if quota_snapshot is not None else provider_quota_snapshot(
        provider_name, config=cfg, state_dir=state)
    if quota is not None:
        used = float(quota.get("used_percent") or 0.0)
        reset = str(quota.get("reset_at") or "unknown reset")
        if background and used >= float(cfg["quota"]["background_block_percent"]):
            _deny(
                state, cfg, code="provider_quota_background", provider=provider_name,
                session_id=session,
                message=f"Background {provider_name} call blocked at {used:.1f}% provider quota; reset {reset}.",
            )
        if not background and used >= float(cfg["quota"]["foreground_fallback_percent"]):
            _deny(
                state, cfg, code="provider_quota_foreground", provider=provider_name,
                session_id=session,
                message=f"Foreground {provider_name} call diverted at {used:.1f}% provider quota; reset {reset}.",
                fallback_recommended=True,
            )

    now = time.time()
    hour = now - 3600
    day = now - 86400
    reserve = _estimated_tokens(request)
    digest = _request_digest(request)
    request_id = uuid.uuid4().hex
    try:
        conn = _connect(state)
        conn.execute("BEGIN IMMEDIATE")
        stale_before = now - _int_setting(cfg, "reservation_stale_seconds")
        conn.execute(
            "UPDATE requests SET status='abandoned' "
            "WHERE status='reserved' AND ts<?",
            (stale_before,),
        )
        provider_cfg = cfg["provider"]
        session_cfg = cfg["session"]
        background_cfg = cfg["background"]
        checks: list[tuple[bool, str, str, bool]] = []

        provider_calls_hour = _sum(conn, "provider=? AND ts>=?", (provider_name, hour))
        provider_calls_day = _sum(conn, "provider=? AND ts>=?", (provider_name, day))
        provider_tokens_hour = _sum(conn, "provider=? AND ts>=?", (provider_name, hour), tokens=True)
        provider_tokens_day = _sum(conn, "provider=? AND ts>=?", (provider_name, day), tokens=True)
        session_calls = _sum(conn, "session_id=?", (session,))
        session_tokens = _sum(conn, "session_id=?", (session,), tokens=True)
        repeated = _sum(conn, "session_id=? AND digest=? AND ts>=?", (session, digest, hour))

        checks.extend([
            (provider_calls_hour >= _int_setting(provider_cfg, "max_calls_per_hour"),
             "provider_calls_hour", "hourly provider call budget", True),
            (provider_calls_day >= _int_setting(provider_cfg, "max_calls_per_day"),
             "provider_calls_day", "daily provider call budget", True),
            (provider_tokens_hour + reserve > _int_setting(provider_cfg, "max_tokens_per_hour"),
             "provider_tokens_hour", "hourly provider token budget", True),
            (provider_tokens_day + reserve > _int_setting(provider_cfg, "max_tokens_per_day"),
             "provider_tokens_day", "daily provider token budget", True),
            (session_calls >= _int_setting(session_cfg, "max_calls"),
             "session_calls", "session call budget", False),
            (session_tokens + reserve > _int_setting(session_cfg, "max_total_tokens"),
             "session_tokens", "session token budget", False),
            (repeated >= _int_setting(session_cfg, "repeated_request_limit"),
             "repeated_request", "repeated-context request budget", False),
        ])
        if background:
            active = _sum(conn, "background=1 AND status='reserved'", ())
            bg_calls_hour = _sum(conn, "background=1 AND ts>=?", (hour,))
            bg_calls_day = _sum(conn, "background=1 AND ts>=?", (day,))
            bg_tokens_hour = _sum(conn, "background=1 AND ts>=?", (hour,), tokens=True)
            bg_tokens_day = _sum(conn, "background=1 AND ts>=?", (day,), tokens=True)
            checks.extend([
                (active >= _int_setting(background_cfg, "max_concurrent"),
                 "background_concurrent", "background concurrency budget", False),
                (bg_calls_hour >= _int_setting(background_cfg, "max_calls_per_hour"),
                 "background_calls_hour", "hourly background call budget", False),
                (bg_calls_day >= _int_setting(background_cfg, "max_calls_per_day"),
                 "background_calls_day", "daily background call budget", False),
                (bg_tokens_hour + reserve > _int_setting(background_cfg, "max_tokens_per_hour"),
                 "background_tokens_hour", "hourly background token budget", False),
                (bg_tokens_day + reserve > _int_setting(background_cfg, "max_tokens_per_day"),
                 "background_tokens_day", "daily background token budget", False),
            ])
        for denied, code, label, can_fallback in checks:
            if denied:
                conn.rollback()
                suffix = " Start a new session or compact before retrying." if code.startswith("session_") or code == "repeated_request" else ""
                _deny(
                    state, cfg, code=code, provider=provider_name, session_id=session,
                    message=f"Model call blocked by hard local {label}.{suffix}",
                    fallback_recommended=(can_fallback and not background),
                )

        conn.execute(
            "INSERT INTO requests(request_id,ts,provider,model,session_id,background,role,task,digest,reserved_tokens,actual_tokens,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'reserved')",
            (
                request_id, now, provider_name, model_name, session, int(background),
                str(role or "primary"), str(task or ""), digest, reserve,
            ),
        )
        conn.commit()
    except BudgetExceeded:
        raise
    except Exception as exc:
        if cfg.get("fail_closed", True):
            _deny(
                state, cfg, code="ledger_unavailable", provider=provider_name, session_id=session,
                message=f"Model spend ledger unavailable; failing closed ({type(exc).__name__}).",
            )
        logger.exception("Model spend ledger unavailable; fail_open configured")
    finally:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
    return RequestLease(request_id, state, provider_name, model_name, session, background, reserve)


def _response_total_tokens(response: Any, provider: str) -> Optional[int]:
    raw = getattr(response, "usage", None)
    if raw is None and isinstance(response, Mapping):
        raw = response.get("usage")
    if raw is None:
        return None
    try:
        from agent.usage_pricing import normalize_usage
        usage = normalize_usage(raw, provider=provider)
        return int(
            usage.input_tokens + usage.output_tokens
            + usage.cache_read_tokens + usage.cache_write_tokens + usage.reasoning_tokens
        )
    except Exception:
        return None


def _complete_request(lease: RequestLease, response: Any = None, *, status: str = "ok") -> None:
    if lease.reserved_tokens <= 0:
        return
    actual = _response_total_tokens(response, lease.provider)
    try:
        with _connect(lease.state_dir) as conn:
            conn.execute(
                "UPDATE requests SET actual_tokens=?, status=? WHERE request_id=?",
                (actual, status, lease.request_id),
            )
    except Exception:
        # A postflight failure must not erase the conservative reservation. Preflight remains
        # fail-closed on the next request and the operator ledger shows a stuck reservation.
        logger.exception("Model spend postflight accounting failed for %s", lease.request_id)


def guarded_model_call(
    request: Mapping[str, Any],
    next_call: Callable[[Mapping[str, Any]], T],
    *,
    provider: Optional[str],
    model: Optional[str],
    session_id: Optional[str],
    platform: Optional[str],
    role: Optional[str],
    task: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    state_dir: Optional[Path] = None,
    quota_snapshot: Optional[Mapping[str, Any]] = None,
) -> T:
    lease = preflight_request(
        request, provider=provider, model=model, session_id=session_id, platform=platform,
        role=role, task=task, config=config, state_dir=state_dir, quota_snapshot=quota_snapshot,
    )
    try:
        response = next_call(request)
    except BaseException:
        lease.complete(status="error")
        raise
    lease.complete(response=response, status="ok")
    return response


async def guarded_model_call_async(
    request: Mapping[str, Any],
    next_call: Callable[[Mapping[str, Any]], Awaitable[T]],
    *,
    provider: Optional[str],
    model: Optional[str],
    session_id: Optional[str],
    platform: Optional[str],
    role: Optional[str],
    task: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    state_dir: Optional[Path] = None,
    quota_snapshot: Optional[Mapping[str, Any]] = None,
) -> T:
    lease = preflight_request(
        request, provider=provider, model=model, session_id=session_id, platform=platform,
        role=role, task=task, config=config, state_dir=state_dir, quota_snapshot=quota_snapshot,
    )
    try:
        response = await next_call(request)
    except BaseException:
        lease.complete(status="error")
        raise
    lease.complete(response=response, status="ok")
    return response
