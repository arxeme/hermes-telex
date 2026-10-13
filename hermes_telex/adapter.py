"""TelexAdapter + plugin registration.

Registers the ``telex`` platform via the modern Plugin Path
(``ctx.register_platform`` + hooks); no monkey-patching.

Access policy is enforced in-plugin. Like hermes-seatalk, the plugin is the
sole access authority and the gateway's own gate steps aside via an internal
allow-all flag — which also matches openclaw, where the plugin is the only
authority and there is no host-side second gate at all.

The one case that cannot use allow-all is ``dm_policy=pairing``: the gateway
issues a pairing code only when its authorization check *fails*, so allow-all
would silently authorize unpaired senders. Accounts that delegate pairing keep
the ``enforces_own_access_policy`` carve-out instead. See
:func:`_gateway_authorization_mode`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from . import accounts as acct
from . import config as cfg
from . import interactive
from . import send as sendmod
from . import targets as targetmod
from .client import TelexClient, get_telex_client
from .dispatcher import TelexDispatcher
from .log import get_logger, mask_key
from .monitor import run_monitor
from .types import DEFAULT_BASE_URL, ConversationKind

logger = get_logger("adapter")

TELEX_PLATFORM = "telex"
TELEX_PLUGIN_NAME = "telex-platform"
MAX_MESSAGE_LENGTH = sendmod.DEFAULT_TEXT_CHUNK_LIMIT

# Internal — never written to the user's .env. Registered as this platform's
# allow_all_env so the gateway defers access control to this plugin. Mirrors
# hermes-seatalk's HERMES_SEATALK_ALLOW_ALL.
INTERNAL_ALLOW_ALL_ENV = "HERMES_TELEX_ALLOW_ALL"

# hermes base (fallback stubs let this import + unit-test outside hermes).
try:  # pragma: no cover - real base inside hermes
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    _HAS_HERMES_BASE = True
except Exception:  # pragma: no cover
    _HAS_HERMES_BASE = False

    class Platform(str):  # type: ignore
        pass

    class BasePlatformAdapter:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

        def build_source(self, **kwargs):
            return kwargs

        async def handle_message(self, event):
            return None

        def resume_typing_for_chat(self, chat_id):
            return None

    @dataclass
    class SendResult:  # type: ignore
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: Any = None
        retryable: bool = False


def _extra(config: Any) -> dict:
    extra = getattr(config, "extra", None)
    extra = dict(extra) if isinstance(extra, dict) else {}
    # env quickstart seeds a single default account when YAML is absent.
    if not extra.get("api_key") and not extra.get("accounts"):
        env_extra = cfg.config_from_env()
        if env_extra:
            extra.update(env_extra)
    return extra


@dataclass
class TelexAccountRuntime:
    account: acct.ResolvedTelexAccount
    client: TelexClient
    dispatcher: TelexDispatcher | None = None
    monitor_task: asyncio.Task | None = None
    # Created in connect() (inside the running loop) — never eagerly in the
    # sync constructor, where no event loop may exist.
    stop_event: asyncio.Event | None = None


def _gateway_allow_all(enabled: list) -> bool:
    """Whether the gateway's own access gate should stand down for this config.

    True → set the internal allow-all flag: this plugin is the sole access
    authority, exactly like hermes-seatalk (and like openclaw, which has no
    host-side gate at all).

    False → leave the gateway gate active. Required for ``dm_policy=pairing``:
    ``gateway/run.py`` offers a pairing code only when
    ``_is_user_authorized()`` returns False, and allow-all is checked before
    the pairing store, so an unpaired sender would be authorized silently.
    """
    if not enabled:
        return False
    return not any(a.dm_policy == "pairing" for a in enabled)


def _gateway_group_policy(account: Any) -> str:
    """Group policy advertised to the gateway's adapter-trust carve-out.

    Only consulted when allow-all is off (a pairing account). The gateway
    trusts an own-policy adapter for group traffic only when the effective
    policy is ``allowlist``; it deliberately refuses to trust ``open``, which
    was a fail-open. Report ``allowlist`` when this account really does gate
    group traffic — by channel or by sender — so channels are not blanket
    denied while the guard still catches a genuinely ungated ``open``.
    """
    if account.group_policy == "allowlist" or account.group_sender_allow_from:
        return "allowlist"
    return account.group_policy


class TelexAdapter(BasePlatformAdapter):
    # Kept for the pairing path, where allow-all must stay off: it lets the
    # gateway trust our own dm/group decisions instead of default-denying.
    # Under allow-all this is moot (that flag is checked first).
    enforces_own_access_policy = True

    def __init__(self, config: Any):
        platform = Platform(TELEX_PLATFORM)
        try:
            super().__init__(config=config, platform=platform)
        except TypeError:  # pragma: no cover - fallback base
            super().__init__()
        self.config = config
        self.extra = _extra(config)
        self._runtimes: dict[str, TelexAccountRuntime] = {}
        enabled = acct.list_enabled_accounts(self.extra)
        for account in enabled:
            client = get_telex_client(account.api_key, account.base_url, account.bot_id)
            self._runtimes[account.account_id] = TelexAccountRuntime(account=account, client=client)
        # Gateway authorization handoff (see module docstring). Written on every
        # construction so a stale value from an earlier config can't linger in
        # the process.
        os.environ[INTERNAL_ALLOW_ALL_ENV] = "true" if _gateway_allow_all(enabled) else "false"
        # Still advertised for the pairing path: with allow-all off the gateway
        # reads these to carve pairing out of the adapter-trust shortcut
        # (authz_mixin._adapter_dm_policy / _adapter_group_policy).
        primary = enabled[0] if enabled else acct.resolve_account(
            self.extra, acct.DEFAULT_ACCOUNT_ID
        )
        self._dm_policy = primary.dm_policy
        self._group_policy = _gateway_group_policy(primary)
        self._interactions = interactive.InteractionRouter(self.send, self.resume_typing_for_chat)

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False, **_kwargs) -> bool:
        # Newer hermes builds always pass is_reconnect (gateway/run.py calls
        # adapter.connect(is_reconnect=...)); older builds call bare connect().
        if not self._runtimes:
            logger.warning("no enabled/configured Telex accounts; nothing to connect")
            return False
        for account_id, rt in self._runtimes.items():
            # Reconnect re-entry: stop a previous monitor before starting a new
            # one, or each retry would stack another subscribe loop.
            if rt.monitor_task is not None and not rt.monitor_task.done():
                if rt.stop_event is not None:
                    rt.stop_event.set()
                rt.monitor_task.cancel()
                try:
                    await rt.monitor_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            rt.client.arm_self_id(rt.account.bot_id)
            rt.dispatcher = TelexDispatcher(self, rt.account, rt.client)
            rt.stop_event = asyncio.Event()
            rt.monitor_task = asyncio.create_task(
                run_monitor(
                    rt.client,
                    partial(self._handle_inbound, rt),
                    rt.stop_event,
                    account_id=account_id,
                )
            )
            logger.info(
                "connected account=%s base_url=%s key=%s dm_policy=%s",
                account_id, rt.account.base_url, mask_key(rt.account.api_key), rt.account.dm_policy,
            )
        return True

    async def disconnect(self) -> None:
        for rt in self._runtimes.values():
            if rt.stop_event is not None:
                rt.stop_event.set()
            if rt.monitor_task and not rt.monitor_task.done():
                rt.monitor_task.cancel()
                try:
                    await rt.monitor_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await rt.client.close()

    # -- outbound -----------------------------------------------------------

    def _runtime_for(self, metadata: dict | None) -> TelexAccountRuntime | None:
        if not self._runtimes:
            return None
        if metadata:
            aid = metadata.get("telex_account_id") or metadata.get("_telex_account_id")
            if aid and aid in self._runtimes:
                return self._runtimes[aid]
        return next(iter(self._runtimes.values()))

    async def _resolve_target(self, chat_id: str, rt: TelexAccountRuntime) -> tuple[str | None, str | None]:
        """Return (conversation_id, peer_id) for a send target."""
        target = targetmod.parse_target(chat_id)
        if target is None:
            return None, None
        if target.kind == "conversation":
            return target.value, None
        if target.kind == "peer":
            return None, target.value
        # email -> resolve to identity -> peer_id
        identities = await rt.client.get_identities([], [target.value])
        if not identities:
            raise ValueError(
                f"Telex: no identity found for email '{target.value}'. "
                "Check the email is correct and the account exists."
            )
        return None, identities[0]["id"]

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict | None = None) -> SendResult:
        del reply_to
        rt = self._runtime_for(metadata)
        if rt is None:
            return SendResult(success=False, error="No Telex account configured")
        try:
            conversation_id, peer_id = await self._resolve_target(chat_id, rt)
            if not conversation_id and not peer_id:
                return SendResult(success=False, error=f"invalid Telex target: {chat_id}")
            message = await sendmod.send_telex_message(
                rt.client, conversation_id=conversation_id, peer_id=peer_id,
                text=content, chunk_limit=MAX_MESSAGE_LENGTH,
            )
            return SendResult(success=True, message_id=(message or {}).get("id"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("send failed chat_id=%s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata: dict | None = None) -> SendResult:
        rt = self._runtime_for(metadata)
        if rt is None or rt.account.processing_indicator == "off":
            return SendResult(success=True)
        try:
            conversation_id, _peer = await self._resolve_target(chat_id, rt)
            if conversation_id:
                await rt.client.set_activity(conversation_id, "processing")
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=str(exc))

    async def _send_media(self, chat_id: str, path: str, kind: str, caption: str,
                          metadata: dict | None) -> SendResult:
        rt = self._runtime_for(metadata)
        if rt is None:
            return SendResult(success=False, error="No Telex account configured")
        try:
            conversation_id, peer_id = await self._resolve_target(chat_id, rt)
            message = await sendmod.send_telex_message(
                rt.client, conversation_id=conversation_id, peer_id=peer_id,
                text=caption or None, media_units=[(path, kind)], chunk_limit=MAX_MESSAGE_LENGTH,
            )
            return SendResult(success=True, message_id=(message or {}).get("id"))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=str(exc))

    async def send_image(self, chat_id: str, image_url: str, caption: str | None = None,
                         reply_to: str | None = None, metadata: dict | None = None) -> SendResult:
        return await self._send_media(chat_id, image_url, "image", caption or "", metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: str | None = None,
                              reply_to: str | None = None, metadata: dict | None = None, **kw) -> SendResult:
        return await self._send_media(chat_id, image_path, "image", caption or "", metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: str | None = None,
                            file_name: str | None = None, reply_to: str | None = None,
                            metadata: dict | None = None, **kw) -> SendResult:
        return await self._send_media(chat_id, file_path, "document", caption or "", metadata)

    # -- gateway prompts ----------------------------------------------------

    async def _handle_inbound(self, rt: TelexAccountRuntime, message: dict[str, Any]) -> None:
        answered = interactive.answered_details(message, rt.client.self_id)
        if answered is None:
            await rt.dispatcher.handle(message)
        else:
            await self._interactions.handle(rt.client, rt.dispatcher, answered)

    async def _answerer_ids(self, rt: TelexAccountRuntime, conversation_id: str | None) -> list[str]:
        """Outside a Channel's sender allowlist a member's answer is refused, but an open card would let
        that member close it for everyone; Telex takes only members as answerers."""
        allowed = rt.account.group_sender_allow_from
        if not conversation_id or not allowed or "*" in allowed:
            return []
        conversation = await rt.client.get_conversation(conversation_id)
        if conversation.get("kind") != ConversationKind.CHANNEL:
            return []
        ids = [entry for entry in allowed if "@" not in entry]
        emails = [entry for entry in allowed if "@" in entry]
        if emails:
            ids += [identity["id"] for identity in await rt.client.get_identities([], emails)]
        members = {member["identity_id"] for member in await rt.client.list_members(conversation_id)}
        listed = [identity_id for identity_id in dict.fromkeys(ids) if identity_id in members]
        if not listed:
            raise ValueError("group_sender_allow_from matches no channel member")
        return listed

    async def _send_prompt(
        self,
        chat_id: str,
        metadata: dict | None,
        build: Callable[[list[str], str | None], list[dict[str, Any]]],
        prompt: interactive.Prompt,
        timeout_s: int,
    ) -> SendResult:
        rt = self._runtime_for(metadata)
        if rt is None:
            return SendResult(success=False, error="no Telex account for this chat")
        try:
            conversation_id, peer_id = await self._resolve_target(chat_id, rt)
            if not conversation_id and not peer_id:
                return SendResult(success=False, error="chat_id names no Telex conversation")
            answerer_ids = await self._answerer_ids(rt, conversation_id)
            message = await rt.client.send_message(
                conversation_id=conversation_id,
                peer_id=peer_id,
                blocks=build(answerer_ids, interactive.deadline_after(timeout_s)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s card failed, falling back to text chat_id=%s: %s", prompt.kind, chat_id, exc)
            return SendResult(success=False, error=f"Telex {prompt.kind} card not sent")
        prompt.account_id = rt.account.account_id
        prompt.conversation_id = message.get("conversation_id") or conversation_id or ""
        self._interactions.remember(message, prompt)
        return SendResult(success=True, message_id=message.get("id"))

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list | None,
        clarify_id: str,
        session_key: str,
        metadata: dict | None = None,
    ) -> SendResult:
        prompt = interactive.Prompt(
            kind="clarify", interaction_id=clarify_id, question=question.strip(),
            choices=tuple(str(choice).strip() for choice in choices or []),
            multi_select=interactive.clarify_multi_select(clarify_id),
        )
        return await self._send_prompt(
            chat_id, metadata,
            lambda answerer_ids, deadline: interactive.clarify_blocks(prompt, answerer_ids, deadline),
            prompt, interactive.clarify_timeout_s(),
        )

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        del notice
        retired = self._interactions.take_clarify(clarify_id)
        if retired is None:
            return
        message_id, prompt = retired
        rt = self._runtimes.get(prompt.account_id)
        if rt is not None:
            await rt.client.close_interaction(message_id, prompt.interaction_id)

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        """A failed SendResult is the gateway's signal to send its own text prompt."""
        request_id = self._interactions.claim_approval(prompt.session_key, prompt.command)
        if not request_id:
            return SendResult(success=False, error="no pending approval to render")
        card = interactive.Prompt(
            kind="approval", interaction_id=interactive.new_interaction_id(), session_key=prompt.session_key,
            request_id=request_id,
        )
        try:
            return await self._send_prompt(
                prompt.chat_id, prompt.metadata,
                lambda answerer_ids, deadline: interactive.approval_blocks(
                    card.interaction_id, prompt.text, prompt.actions, answerer_ids, deadline
                ),
                card, interactive.approval_timeout_s(),
            )
        finally:
            self._interactions.release_approval(request_id)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict | None = None,
    ) -> SendResult:
        prompt = interactive.Prompt(
            kind="confirm", interaction_id=interactive.new_interaction_id(),
            session_key=session_key, confirm_id=confirm_id,
        )
        text = f"**{title}**\n\n{message}" if title else message
        return await self._send_prompt(
            chat_id, metadata,
            lambda answerer_ids, deadline: interactive.confirm_blocks(
                prompt.interaction_id, text, answerer_ids, deadline
            ),
            prompt, interactive.confirm_timeout_s(),
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        rt = self._runtime_for(None)
        if rt is None:
            return {"chat_id": chat_id, "name": "", "type": "unknown"}
        target = targetmod.parse_target(chat_id)
        conv_id = target.value if target and target.kind == "conversation" else chat_id
        try:
            conv = await rt.client.get_conversation(conv_id)
            ctype = "channel" if conv.get("kind") == ConversationKind.CHANNEL else "chat"
            return {"chat_id": chat_id, "name": conv.get("title", ""), "type": ctype}
        except Exception:  # noqa: BLE001
            return {"chat_id": chat_id, "name": "", "type": "unknown"}


# -- platform callbacks -----------------------------------------------------

def check_telex_requirements() -> bool:
    # Dependency check only. Config presence (env OR config.yaml accounts) is
    # verified by validate_config / is_connected, which read PlatformConfig.extra.
    # Requiring TELEX_API_KEY here would wrongly fail YAML-configured deployments.
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _validate_telex_config(config: Any) -> bool:
    extra = _extra(config)
    for account in acct.resolve_all_accounts(extra):
        if account.configured:
            try:
                cfg.validate_account(account.__dict__, account.account_id)
            except cfg.TelexConfigError as exc:
                logger.warning("invalid Telex config: %s", exc)
                return False
            return True
    return bool(os.getenv("TELEX_API_KEY"))


def _is_telex_connected(config: Any) -> bool:
    return _validate_telex_config(config)


def _telex_env_enablement() -> dict | None:
    """Seed PlatformConfig from env BEFORE adapter construction.

    The core hook merges the returned FLAT keys into ``extra`` and pulls the
    special ``home_channel`` key into a HomeChannel dataclass. Must surface the
    home channel even when the api_key lives in config.yaml (not env) — otherwise
    ``/sethome`` (which writes TELEX_HOME_CHANNEL to ~/.hermes/.env) is ignored by
    the send_message home-channel path.
    """
    seed: dict = {}
    env_extra = cfg.config_from_env()  # flat single-account fields, or None
    if env_extra:
        seed.update(env_extra)
    home = os.getenv("TELEX_HOME_CHANNEL", "").strip()
    if home:
        hc: dict = {"chat_id": home, "name": os.getenv("TELEX_HOME_CHANNEL_NAME", "Telex Home")}
        thread = os.getenv("TELEX_HOME_CHANNEL_THREAD_ID", "").strip()
        if thread:
            hc["thread_id"] = thread
        seed["home_channel"] = hc
    return seed or None


async def _telex_standalone_send(pconfig, chat_id, message, *, thread_id=None,
                                 media_files=None, force_document=False) -> dict:
    """Out-of-process delivery for cron jobs (no live adapter). thread_id ignored."""
    del thread_id
    extra = _extra(pconfig)
    enabled = acct.list_enabled_accounts(extra)
    if not enabled:
        return {"error": "No Telex account configured"}
    account = enabled[0]
    client = get_telex_client(account.api_key, account.base_url, account.bot_id)
    try:
        target = targetmod.parse_target(chat_id)
        conversation_id = target.value if target and target.kind == "conversation" else None
        peer_id = target.value if target and target.kind == "peer" else None
        if target and target.kind == "email":
            ids = await client.get_identities([], [target.value])
            if not ids:
                return {"error": f"no identity for email {target.value}"}
            peer_id = ids[0]["id"]
        units = [(p, "document" if force_document else _kind_for(p)) for p, _v in (media_files or [])]
        result = await sendmod.send_telex_message(
            client, conversation_id=conversation_id, peer_id=peer_id,
            text=message or None, media_units=units or None, chunk_limit=MAX_MESSAGE_LENGTH,
        )
        return {"success": True, "message_id": (result or {}).get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    finally:
        await client.close()


def _kind_for(path: str) -> str:
    from .media import kind_for_path
    return kind_for_path(path)


_TELEX_PLATFORM_HINT = (
    "You are chatting via Telex (Voyager's chat product). Markdown is supported. "
    "To @-mention someone, write the inline token [@](mention:<identity_id>) "
    "(16-char hex id from the telex tool or message context); [@all](mention:all) notifies "
    "everyone; the server fills in the display name. Plain @name text does not notify anyone. "
    "Text replies deliver automatically — answering the message you are handling needs no tool call. "
    "To send a file or image, write MEDIA:<absolute path> on its own line. "
    "To post into any other Telex conversation — including a channel you just created — use the "
    'telex tool: telex(action="send_message", conversation_id="<16-hex id>", text="..."), or '
    "peer_id/email for a 1:1. Use it rather than the core send_message tool for Telex targets: "
    "core resolves only conversations hermes has already seen, so it fails on new ones. "
    "To answer an <interaction> listing message_id and interaction_id, use the telex tool's "
    "answer_interaction action. "
    "Keep replies concise and conversational."
)


def register(ctx) -> None:
    """Plugin entry point called by the hermes-agent plugin loader."""
    # Fail closed until the adapter resolves the accounts and decides: this also
    # prevents an externally exported HERMES_TELEX_ALLOW_ALL from granting
    # blanket access, since the flag is internal and not a user-facing setting.
    os.environ[INTERNAL_ALLOW_ALL_ENV] = "false"
    ctx.register_platform(
        name=TELEX_PLATFORM,
        label="Telex",
        adapter_factory=lambda cfg_: TelexAdapter(cfg_),
        check_fn=check_telex_requirements,
        validate_config=_validate_telex_config,
        is_connected=_is_telex_connected,
        required_env=["TELEX_API_KEY"],
        install_hint="No extra packages needed (aiohttp ships with Hermes)",
        env_enablement_fn=_telex_env_enablement,
        cron_deliver_env_var="TELEX_HOME_CHANNEL",
        standalone_sender_fn=_telex_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📨",
        platform_hint=_TELEX_PLATFORM_HINT,
        # Access is enforced in-plugin. The adapter sets this flag from the
        # resolved accounts (off for pairing); no allowed_users_env.
        allow_all_env=INTERNAL_ALLOW_ALL_ENV,
    )
    try:
        from .tools import register_telex_tool
        register_telex_tool(ctx)
    except Exception as exc:  # noqa: BLE001 - tool is optional
        logger.debug("telex tool registration skipped: %s", exc)
