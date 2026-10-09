"""Gateway prompts as Telex interactions: clarify, dangerous-command approval, slash confirm.

Each sent prompt is remembered in-process because the approval and confirm resolvers are keyed by a
hermes session key, which the message cannot carry.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import blocks
from .client import TelexClient
from .dispatcher import TelexDispatcher
from .log import get_logger
from .types import InteractionButtonStyle, InteractionMode, InteractionStatus, MessageFlag

logger = get_logger("interactive")

ANSWERED_EVENT = "interaction_answered"
_APPROVAL_STYLES = {"primary": InteractionButtonStyle.PRIMARY, "danger": InteractionButtonStyle.DANGER}


@dataclass
class Prompt:
    kind: str
    interaction_id: str
    expires_at: float = 0.0
    account_id: str = ""
    conversation_id: str = ""
    question: str = ""
    choices: tuple[str, ...] = ()
    multi_select: bool = False
    session_key: str = ""
    confirm_id: str = ""
    request_id: str = ""


# -- hermes state ------------------------------------------------------------

def _clarify_entry(clarify_id: str) -> Any:
    try:
        from tools import clarify_gateway

        with clarify_gateway._lock:
            return clarify_gateway._entries.get(clarify_id)
    except Exception:  # noqa: BLE001
        return None


def clarify_multi_select(clarify_id: str) -> bool:
    """The flag reaches no adapter through ``send_clarify``; the base class reads it the same way."""
    return bool(getattr(_clarify_entry(clarify_id), "multi_select", False))


def clarify_timeout_s() -> int:
    try:
        from tools import clarify_gateway

        return int(clarify_gateway.get_clarify_timeout())
    except Exception:  # noqa: BLE001
        return 0


def approval_timeout_s() -> int:
    try:
        from gateway.platforms.base_exec_approval import approval_timeout_seconds

        return int(approval_timeout_seconds())
    except Exception:  # noqa: BLE001
        return 0


def confirm_timeout_s() -> int:
    try:
        from tools import slash_confirm

        return int(slash_confirm.DEFAULT_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        return 0


def _confirm_pending(prompt: Prompt) -> bool:
    try:
        from tools import slash_confirm

        pending = slash_confirm.get_pending(prompt.session_key)
    except Exception:  # noqa: BLE001
        return False
    return bool(pending) and pending.get("confirm_id") == prompt.confirm_id


def pending_approval(session_key: str, command: str, taken: set[str]) -> str:
    """The prompt a card renders carries no request_id, only the command as the gateway redacted it for
    display, and parallel tool calls can queue several approvals at once."""
    try:
        from gateway.run import _redact_approval_command
        from tools import approval

        entries = approval.list_gateway_approvals(session_key)
    except Exception:  # noqa: BLE001
        return ""
    for entry in entries:
        if entry["request_id"] not in taken and _redact_approval_command(entry.get("command", "")) == command:
            return entry["request_id"]
    return ""


# -- blocks ------------------------------------------------------------------

def deadline_after(seconds: int) -> str | None:
    if seconds <= 0:
        return None
    at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _present(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v}


def _interaction(
    interaction_id: str, mode: int, question: dict[str, Any], answerer_ids: list[str], deadline: str | None
) -> dict[str, Any]:
    return {
        "id": interaction_id, "mode": mode, "questions": [question],
        **_present(answerer_ids=answerer_ids, deadline=deadline),
    }


def clarify_blocks(prompt: Prompt, answerer_ids: list[str], deadline: str | None) -> list[dict[str, Any]]:
    options = [{"id": str(index), "label": label} for index, label in enumerate(prompt.choices)]
    question = _present(text=prompt.question, options=options, multi_select=prompt.multi_select, allow_free_text=True)
    return [blocks.interaction_block(_interaction(
        prompt.interaction_id, InteractionMode.SELECT, question, answerer_ids, deadline,
    ))]


def _button(option_id: str, label: str, style: int = InteractionButtonStyle.DEFAULT) -> dict[str, Any]:
    return _present(id=option_id, label=label, style=style)


def _button_blocks(
    interaction_id: str, text: str, buttons: list[dict[str, Any]], answerer_ids: list[str], deadline: str | None
) -> list[dict[str, Any]]:
    return [
        blocks.text_block(text),
        blocks.interaction_block(_interaction(
            interaction_id, InteractionMode.BUTTON, {"options": buttons}, answerer_ids, deadline,
        )),
    ]


def approval_blocks(
    interaction_id: str,
    text: str,
    actions: list[tuple[str, str, str]],
    answerer_ids: list[str],
    deadline: str | None,
) -> list[dict[str, Any]]:
    buttons = [
        _button(choice, label, _APPROVAL_STYLES.get(style, InteractionButtonStyle.DEFAULT))
        for label, choice, style in actions
    ]
    return _button_blocks(interaction_id, text, buttons, answerer_ids, deadline)


def confirm_blocks(
    interaction_id: str, text: str, answerer_ids: list[str], deadline: str | None
) -> list[dict[str, Any]]:
    buttons = [
        _button("once", "Approve once", InteractionButtonStyle.PRIMARY),
        _button("always", "Always approve"),
        _button("cancel", "Cancel", InteractionButtonStyle.DANGER),
    ]
    return _button_blocks(interaction_id, text, buttons, answerer_ids, deadline)


def new_interaction_id() -> str:
    return uuid.uuid4().hex[:12]


# -- answers -----------------------------------------------------------------

def answered_details(message: dict[str, Any], self_id: str | None) -> dict[str, Any] | None:
    flags = message.get("flags", 0)
    # A fork copies the events of its source.
    if not self_id or not flags & MessageFlag.EVENT or flags & MessageFlag.FORK_PREFIX:
        return None
    event = blocks.event(message)
    details = event.get("details") or {}
    if event.get("kind") != ANSWERED_EVENT or details.get("asker_id") != self_id:
        return None
    return details


def clarify_response(prompt: Prompt, answer: dict[str, Any]) -> str:
    """Hermes reads a multi-select answer from a JSON array of labels."""
    parts = [
        prompt.choices[int(option_id)]
        for option_id in answer.get("option_ids") or []
        if option_id.isdigit() and int(option_id) < len(prompt.choices)
    ]
    text = (answer.get("text") or "").strip()
    if text:
        parts.append(text)
    if prompt.multi_select:
        return json.dumps(parts, ensure_ascii=False)
    return parts[0] if parts else ""


class InteractionRouter:
    def __init__(
        self,
        send_text: Callable[..., Awaitable[Any]],
        resume_typing: Callable[[str], None],
    ):
        self._send_text = send_text
        self._resume_typing = resume_typing
        self._prompts: dict[str, Prompt] = {}
        self._claiming: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def remember(self, message: dict[str, Any], prompt: Prompt) -> None:
        now = time.time()
        for message_id in [mid for mid, waiting in self._prompts.items() if waiting.expires_at <= now]:
            del self._prompts[message_id]
        interaction = next(b["interaction"] for b in message["data"]["blocks"] if b.get("interaction"))
        prompt.expires_at = datetime.fromisoformat(interaction["deadline"]).timestamp()
        self._prompts[message.get("id", "")] = prompt

    def take_clarify(self, clarify_id: str) -> tuple[str, Prompt] | None:
        for message_id, prompt in self._prompts.items():
            if prompt.kind == "clarify" and prompt.interaction_id == clarify_id:
                return message_id, self._prompts.pop(message_id)
        return None

    def claim_approval(self, session_key: str, command: str) -> str:
        # A card is remembered only once its send returns, and another card can be sent meanwhile.
        taken = self._claiming | {prompt.request_id for prompt in self._prompts.values() if prompt.request_id}
        request_id = pending_approval(session_key, command, taken)
        if request_id:
            self._claiming.add(request_id)
        return request_id

    def release_approval(self, request_id: str) -> None:
        self._claiming.discard(request_id)

    async def handle(self, client: TelexClient, dispatcher: TelexDispatcher, details: dict[str, Any]) -> None:
        message_id = details.get("message_id", "")
        interaction = details.get("interaction") or {}
        prompt = self._prompts.get(message_id)
        if prompt is None:
            # An answer nobody waits for, as after a restart, must not look accepted; a skipped card
            # already looks settled.
            close = interaction.get("status") == InteractionStatus.ANSWERED
        else:
            close = not await self._accepted(dispatcher, prompt, interaction)
            self._prompts.pop(message_id, None)
        if close:
            await client.close_interaction(message_id, interaction.get("id", ""))

    async def _accepted(self, dispatcher: TelexDispatcher, prompt: Prompt, interaction: dict[str, Any]) -> bool:
        if not await dispatcher.is_allowed(prompt.conversation_id, interaction.get("answerer_id", "")):
            return False
        status = interaction.get("status")
        answer = (interaction.get("questions") or [{}])[0].get("answer") or {}
        if prompt.kind == "clarify":
            return self._accepted_clarify(prompt, status, answer)
        choice = (answer.get("option_ids") or [""])[0]
        if prompt.kind == "approval":
            return self._accepted_approval(prompt, choice)
        return self._accepted_confirm(prompt, choice)

    def _accepted_clarify(self, prompt: Prompt, status: int, answer: dict[str, Any]) -> bool:
        from tools import clarify_gateway

        response = clarify_gateway.SKIPPED if status == InteractionStatus.SKIPPED else clarify_response(prompt, answer)
        if clarify_gateway.resolve_gateway_clarify(prompt.interaction_id, response):
            return True
        return status == InteractionStatus.SKIPPED

    def _accepted_approval(self, prompt: Prompt, choice: str) -> bool:
        from tools import approval

        if not approval.resolve_gateway_approval(prompt.session_key, choice, request_id=prompt.request_id):
            return False
        self._resume_typing(prompt.conversation_id)
        return True

    def _accepted_confirm(self, prompt: Prompt, choice: str) -> bool:
        if not _confirm_pending(prompt):
            return False
        task = asyncio.create_task(self._run_confirm(prompt, choice))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run_confirm(self, prompt: Prompt, choice: str) -> None:
        from tools import slash_confirm

        try:
            result = await slash_confirm.resolve(prompt.session_key, prompt.confirm_id, choice)
            if result:
                await self._send_text(prompt.conversation_id, result, metadata={"telex_account_id": prompt.account_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("slash confirm failed conversation=%s: %s", prompt.conversation_id, exc)
