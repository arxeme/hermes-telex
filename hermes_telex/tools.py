"""The ``telex`` agent tool: Telex lookups and channel management (port of
openclaw-telex tool.ts / tool-schema.ts). NOT for sending — use
send_message(target="telex:...").
Each action can be disabled per account under ``tools.<name>``.
"""

from __future__ import annotations

import json
from typing import Any

from . import accounts as acct
from . import config as cfg
from .client import get_telex_client
from .log import get_logger
from .types import (
    CONVERSATION_KIND_LABELS,
    IDENTITY_KIND_LABELS,
    MEMBER_ROLE_LABELS,
    MESSAGE_STATUS_LABELS,
    message_flag_labels,
)

logger = get_logger("tool")

ACTIONS = (
    "search_identities",
    "get_identities",
    "list_conversations",
    "get_conversation_info",
    "create_channel",
    "list_members",
    "add_members",
    "get_conversation_messages",
)

TELEX_TOOL_SCHEMA: dict[str, Any] = {
    "name": "telex",
    "description": (
        "Telex lookups and channel management (identities/conversations/members/messages). "
        'NOT for sending — use send_message(target="telex:<chat>") to reply. '
        "Actions: search_identities, get_identities, list_conversations, "
        "get_conversation_info, create_channel, list_members, add_members, "
        "get_conversation_messages. The channel management actions "
        "(create_channel, add_members) can be disabled per account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "query": {"type": "string", "description": "search_identities: name/email text"},
            "ids": {"type": "array", "items": {"type": "string"}, "description": "get_identities: identity ids"},
            "emails": {"type": "array", "items": {"type": "string"}, "description": "get_identities/create_channel/add_members: emails"},
            "kind": {"type": "integer", "description": "list_conversations: 0 chat, 1 channel"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer", "description": "page size (1-100)"},
            "conversation_id": {"type": "string", "description": "16-char hex id"},
            "title": {"type": "string", "description": "create_channel: channel title (1-200)"},
            "identity_ids": {"type": "array", "items": {"type": "string"}, "description": "create_channel/add_members: member identity ids"},
            "before_seq": {"type": "integer"},
            "after_seq": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def _resolve_client_and_account():
    """Prefer the live adapter's default runtime; fall back to env config."""
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform
        runner = _gateway_runner_ref()
        adapter = runner.adapters.get(Platform("telex")) if runner else None
        runtimes = getattr(adapter, "_runtimes", None) if adapter else None
        if runtimes:
            # The tool registry passes no per-turn context, so the acting
            # account cannot be inferred; acting as an arbitrary one would
            # read and mutate across accounts.
            if len(runtimes) > 1:
                return None, "ambiguous"
            rt = next(iter(runtimes.values()))
            return rt.client, rt.account
    except Exception:  # noqa: BLE001
        pass
    extra = cfg.config_from_env()
    if not extra:
        return None, None
    enabled = acct.list_enabled_accounts(extra)
    if not enabled:
        return None, None
    if len(enabled) > 1:
        return None, "ambiguous"
    a = enabled[0]
    return get_telex_client(a.api_key, a.base_url, a.bot_id), a


def _mention_token(identity_id: str) -> str:
    # Empty label lets the server inject the current display name.
    return f"[@](mention:{identity_id})"


def _identity_out(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": i.get("id"),
        "kind": IDENTITY_KIND_LABELS.get(i.get("kind"), i.get("kind")),
        "display_name": i.get("display_name"),
        "email": i.get("email"),
        "online": i.get("online"),
        "mention": _mention_token(i.get("id", "")),
    }


def _conversation_out(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "kind": CONVERSATION_KIND_LABELS.get(c.get("kind"), c.get("kind")),
        "title": c.get("title"),
        "member_count": c.get("member_count"),
        "last_seq": c.get("last_seq"),
    }


def _member_out(m: dict[str, Any], idmap: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "identity_id": m.get("identity_id"),
        "role": MEMBER_ROLE_LABELS.get(m.get("role"), m.get("role")),
        "display_name": idmap.get(m.get("identity_id"), {}).get("display_name"),
        "email": idmap.get(m.get("identity_id"), {}).get("email"),
        "mention": _mention_token(m.get("identity_id", "")),
    }


def _message_out(m: dict[str, Any]) -> dict[str, Any]:
    from . import blocks
    return {
        "id": m.get("id"),
        "seq": m.get("seq"),
        "sender_id": m.get("sender_id"),
        "status": MESSAGE_STATUS_LABELS.get(m.get("status"), m.get("status")),
        "flags": message_flag_labels(m.get("flags", 0)),
        "text": blocks.extract_text(m),
    }


# batch-get-identities silently skips unknown emails, so completeness is checked
# here: any unresolved email aborts before the mutation.
async def _resolve_member_ids(client, identity_ids, emails) -> tuple[list[str], str | None]:
    ids = list(dict.fromkeys(identity_ids or []))
    wanted = list(dict.fromkeys(emails or []))
    if wanted:
        identities = await client.get_identities([], wanted)
        by_email = {str(i.get("email", "")).lower(): i["id"] for i in identities if i.get("id")}
        unresolved = [e for e in wanted if e.lower() not in by_email]
        if unresolved:
            return [], f"unresolved emails: {', '.join(unresolved)}"
        for email in wanted:
            if by_email[email.lower()] not in ids:
                ids.append(by_email[email.lower()])
    return ids, None


async def telex_tool_handler(args: dict[str, Any], **_kwargs: Any) -> str:
    # The tool registry invokes handlers as handler(args, **kwargs) — e.g. task_id.
    # Accept and ignore the extra kwargs.
    action = (args or {}).get("action")
    if action not in ACTIONS:
        return json.dumps({"error": f"unknown action: {action}"})
    client, account = _resolve_client_and_account()
    if account == "ambiguous":
        return json.dumps({
            "error": "multiple Telex accounts configured; the tool cannot infer the acting account and is disabled"
        })
    if client is None:
        return json.dumps({"error": "Telex not configured"})
    if account is not None and not account.tools.get(action, True):
        return json.dumps({"error": f"action '{action}' is disabled"})
    try:
        if action == "search_identities":
            res = await client.search_identities(args.get("query", ""), args.get("limit"))
            return json.dumps({"identities": [_identity_out(i) for i in res]})
        if action == "get_identities":
            res = await client.get_identities(args.get("ids") or [], args.get("emails") or [])
            return json.dumps({"identities": [_identity_out(i) for i in res]})
        if action == "list_conversations":
            res = await client.list_conversations(
                kind=args.get("kind"), offset=args.get("offset"), limit=args.get("limit") or 20
            )
            return json.dumps({
                "conversations": [_conversation_out(c) for c in res["conversations"]],
                "total": res["total"],
            })
        if action == "get_conversation_info":
            conv = await client.get_conversation(args["conversation_id"], force_refresh=True)
            return json.dumps({"conversation": _conversation_out(conv)})
        if action == "create_channel":
            ids, err = await _resolve_member_ids(client, args.get("identity_ids"), args.get("emails"))
            if err:
                return json.dumps({"error": err})
            conv = await client.create_channel(args["title"], ids)
            return json.dumps({"conversation": _conversation_out(conv)})
        if action == "list_members":
            members = await client.list_members(args["conversation_id"])
            idmap = await client.resolve_identities([m.get("identity_id") for m in members if m.get("identity_id")])
            return json.dumps({"members": [_member_out(m, idmap) for m in members]})
        if action == "add_members":
            ids, err = await _resolve_member_ids(client, args.get("identity_ids"), args.get("emails"))
            if err:
                return json.dumps({"error": err})
            if not ids:
                return json.dumps({"error": "provide at least one identity_id or email"})
            members = await client.add_members(args["conversation_id"], ids)
            idmap = await client.resolve_identities([m.get("identity_id") for m in members if m.get("identity_id")])
            return json.dumps({"members": [_member_out(m, idmap) for m in members]})
        if action == "get_conversation_messages":
            msgs = await client.list_messages(
                args["conversation_id"], before_seq=args.get("before_seq"),
                after_seq=args.get("after_seq"), limit=args.get("limit") or 50,
            )
            msgs = sorted(msgs, key=lambda m: m.get("seq", 0))
            return json.dumps({"messages": [_message_out(m) for m in msgs]})
    except KeyError as exc:
        return json.dumps({"error": f"missing required arg: {exc}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})
    return json.dumps({"error": "unhandled action"})


def register_telex_tool(ctx) -> None:
    ctx.register_tool(
        name="telex",
        toolset="telex",
        schema=TELEX_TOOL_SCHEMA,
        handler=telex_tool_handler,
        check_fn=lambda: True,
        is_async=True,
        description="Telex lookups and channel management (identities/conversations/members/messages).",
        emoji="📨",
    )
