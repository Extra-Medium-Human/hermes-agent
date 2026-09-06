"""Recovery-message policy for turns resumed after a gateway interruption."""

from typing import Optional


def build_resume_recovery_note(
    reason: Optional[str], message: str = "", *, interactive: bool = True
) -> str:
    """Build the recovery note for an interrupted turn.

    An empty ``message`` denotes the startup auto-resume event. It always
    continues the saved work because acknowledging the restore would complete
    the synthetic turn and clear the durable marker while abandoning the task.
    ``interactive`` remains part of the call contract for adapter compatibility.
    """
    reason_phrase = (
        "a gateway restart"
        if reason == "restart_timeout"
        else "a gateway shutdown"
        if reason == "shutdown_timeout"
        else "a gateway interruption"
    )
    if message:
        resume_guidance = (
            "Address the user's NEW message below FIRST and focus on what the user is asking now."
        )
        tail_guidance = (
            "Do NOT re-execute old tool calls — skip any unfinished work from the conversation history."
        )
    else:
        resume_guidance = (
            "Do NOT emit only a 'session restored' acknowledgement "
            "or ask the user to restate the task. Review the conversation history and "
            "CONTINUE the interrupted task to completion."
        )
        tail_guidance = (
            "Do NOT re-run tool calls whose results already appear in the history. "
            "If a tool call has no recorded result, its effect is UNKNOWN. Inspect "
            "current state before retrying. If the effect cannot be verified and "
            "retrying could duplicate an external or irreversible action, stop and "
            "ask one specific safety question."
        )
    return (
        f"[System note: The previous turn was interrupted by "
        f"{reason_phrase}; the gateway is now back online. "
        f"Any restart/shutdown command in the history has already "
        f"run — do NOT re-execute or verify it. {resume_guidance} {tail_guidance}]"
        + (f"\n\n{message}" if message else "")
    )


def prepare_resume_pending_message(
    reason: Optional[str], message: Optional[str], *, interactive: bool = True
) -> tuple[str, str]:
    """Return model recovery guidance and the user text to persist.

    A synthesized blank event persists the recovery note because a blank user
    row repeatedly triggers the pre-call sanitizer (#86580). Real user text is
    stored without the recovery scaffold while the model receives both.
    """
    recovery_message = build_resume_recovery_note(
        reason, message or "", interactive=interactive
    )
    persist_message = (
        message if isinstance(message, str) and message.strip() else recovery_message
    )
    return recovery_message, persist_message
