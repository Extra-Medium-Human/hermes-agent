"""Narrow host-owned delivery receipt fanout for installed edge plugins."""

from __future__ import annotations

import logging
import threading
from typing import Callable


logger = logging.getLogger(__name__)
DeliveryReceiptCallback = Callable[[dict[str, str]], None]
_callbacks: dict[str, DeliveryReceiptCallback] = {}
_lock = threading.Lock()


def register_delivery_receipt_callback(
    name: str,
    callback: DeliveryReceiptCallback,
) -> None:
    if not name or not callable(callback):
        raise ValueError("delivery receipt callback requires a name and callable")
    with _lock:
        _callbacks[name] = callback


def clear_delivery_receipt_callbacks() -> None:
    """Test isolation helper; production callers should register idempotently by name."""
    with _lock:
        _callbacks.clear()


def emit_delivery_receipt(
    *,
    content: str,
    platform_name: str,
    chat_id: str,
    thread_id: str,
    message_id: str,
) -> None:
    receipt = {
        "content": str(content),
        "platform_name": str(platform_name),
        "chat_id": str(chat_id),
        "thread_id": str(thread_id),
        "message_id": str(message_id),
    }
    with _lock:
        callbacks = tuple(_callbacks.items())
    for name, callback in callbacks:
        try:
            callback(receipt)
        except Exception:
            logger.exception("delivery receipt callback %s failed", name)