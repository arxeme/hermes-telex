"""TelexAdapter + plugin registration.

Registers the ``telex`` platform via the modern Plugin Path
(``ctx.register_platform`` + hooks); no monkey-patching. Access policy is
enforced in-plugin (``enforces_own_access_policy``); ``dm_policy=pairing`` is
delegated to the hermes-agent gateway's built-in pairing handshake.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from . import accounts as acct
from . import config as cfg
from . import send as sendmod
from . import targets as targetmod
from .client import TelexClient, get_telex_client
from .dispatcher import TelexDispatcher
from .log import get_logger, mask_key
from .monitor import run_monitor
from .types import DEFAULT_BASE_URL

logger = get_logger("adapter")

TELEX_PLATFORM = "telex"
TELEX_PLUGIN_NAME = "telex-platform"
MAX_MESSAGE_LENGTH = sendmod.DEFAULT_TEXT_CHUNK_LIMIT

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


class TelexAdapter(BasePlatformAdapter):
    # Access is enforced here (gateway trusts us); pairing DMs are forwarded so
    # the gateway can run its pairing handshake (see _dm_policy below).
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
        # Expose the primary account's DM policy so the gateway can carve out
        # pairing from the adapter-trust shortcut (authz_mixin._adapter_dm_policy).
        self._dm_policy = enabled[0].dm_policy if enabled else \
            acct.resolve_account(self.extra, acct.DEFAULT_ACCOUNT_ID).dm_policy

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> bool:
        if not self._runtimes:
            logger.warning("no enabled/configured Telex accounts; nothing to connect")
            return False
        for account_id, rt in self._runtimes.items():
            rt.client.arm_self_id(rt.account.bot_id)
            rt.dispatcher = TelexDispatcher(self, rt.account, rt.client)
            rt.stop_event = asyncio.Event()
            rt.monitor_task = asyncio.create_task(
                run_monitor(rt.client, rt.dispatcher.handle, rt.stop_event, account_id=account_id)
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

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        rt = self._runtime_for(None)
        if rt is None:
            return {"chat_id": chat_id, "name": "", "type": "unknown"}
        target = targetmod.parse_target(chat_id)
        conv_id = target.value if target and target.kind == "conversation" else chat_id
        try:
            conv = await rt.client.get_conversation(conv_id)
            from .types import ConversationKind
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
    del thread_id, force_document
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
    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    if ext in {".ogg", ".mp3", ".wav", ".m4a"}:
        return "audio"
    return "document"


_TELEX_PLATFORM_HINT = (
    "You are chatting via Telex (Voyager's chat product). Markdown is supported. "
    "In channels you are addressed by @-mention; in direct chats every message is for you. "
    "Keep replies concise and conversational."
)


def register(ctx) -> None:
    """Plugin entry point called by the hermes-agent plugin loader."""
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
        # Access is enforced in-plugin; no allowed_users_env / allow_all_env.
    )
    try:
        from .tools import register_telex_tool
        register_telex_tool(ctx)
    except Exception as exc:  # noqa: BLE001 - tool is optional
        logger.debug("telex tool registration skipped: %s", exc)
