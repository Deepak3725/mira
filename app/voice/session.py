"""Voice turn handling: transcripts in, replies out.

One function per turn: parse the transcript, run it against the
owner's data (memories, projects, messaging outbox), and return a
reply with separate display text and speakable text. Multi-step
commands keep per-user pending state; anything ambiguous becomes a
clarification question, and nothing sends without a yes.

Pending state is in-memory (lost on restart): an honest limit
noted on the voice page. Every turn is audited.
"""

from __future__ import annotations

import re

from .. import twin_mind, twin_setup, twin_update_nl
from ..budget import usage_summary
from ..channels import (
    ChannelError,
    cancel_message,
    draft_message,
    list_accounts,
    list_contacts,
    parse_time_to_utc,
    resolve_recipient,
    schedule_message,
    send_now,
)
from ..conversations import audit, list_for_user
from ..db import get_twin, get_user, list_opportunities
from ..importers import classify
from ..matching import find_matches
from ..memories import (
    MemoryError,
    create_memory,
    delete_memory,
    get_memory,
    list_memories,
    relevant_memories,
    set_status,
)
from ..projects import ProjectError, create_project, list_projects, log_event
from ..request_parse import parse_need
from .. import requests as need_engine
from .commands import channel_word, is_affirm, is_deny, parse_command
from .registry import get_provider
from .types import VoiceError

MAX_HISTORY = 10
SPEAK_LIMIT = 500
CHAT_HISTORY = 6

_sessions: dict[int, dict] = {}

#: Command kinds that must not be swallowed as setup answers.
_SETUP_GUARD_KINDS = frozenset({
    "message", "project", "chat", "update", "teach", "remember",
    "approve_mem", "reject_mem", "find", "opportunities", "need",
    "my_chats", "status", "undo",
})

#: Kinds that interrupt an active twin chat (exit + run).
_CHAT_INTERRUPT_KINDS = frozenset({
    "message", "project", "setup", "update", "find",
    "opportunities", "need", "my_chats",
})

_CHAT_EXIT = re.compile(
    r"^(stop|exit|quit|bye|goodbye|done|that'?s all|that is all|"
    r"end chat|leave|back to commands)[\s.!]*$", re.IGNORECASE)

_CHAT_CORRECT = re.compile(
    r"^(no[,.]|wrong[,.]?|that'?s wrong[,.]?|that is wrong[,.]?|"
    r"incorrect[,.]?|not quite[,.]?|actually[,.])\s*(.*)$",
    re.IGNORECASE | re.DOTALL)


def _state(user_id: int) -> dict:
    return _sessions.setdefault(user_id, {"pending": None,
                                          "history": []})


def reset_sessions() -> None:
    """Drop all pending voice state (tests, and honest restarts)."""
    _sessions.clear()


def _reply(text: str, transcript, *, speak: str | None = None,
           expect_confirmation: bool = False,
           done: bool = True) -> dict:
    shown = (text or "").strip()
    say = shown if speak is None else speak
    return {"text": shown, "speak": say[:SPEAK_LIMIT],
            "expect_confirmation": expect_confirmation,
            "done": done, "provider": transcript.provider,
            "mock": transcript.mock}


def _history(state: dict, kind: str, text: str) -> None:
    state["history"].append({"kind": kind, "text": text[:200]})
    del state["history"][:-MAX_HISTORY]


def _resolve_target(conn, owner_id: int, slots: dict):
    """Find the (account, contact) pair, or raise an honest error."""
    want = slots.get("channel") or ""
    accounts = [a for a in list_accounts(conn, owner_id)
                if a["status"] == "active"]
    if want and want != "mock":
        accounts = [a for a in accounts if a["channel"] == want]
    if not accounts:
        if want:
            raise VoiceError(
                f"You have no active {want} account. Connect one"
                " in Channels first.")
        raise VoiceError("You have no active messaging account."
                         " Connect one in Channels first.")
    hits = []
    for account in accounts:
        contacts = [{"id": c["id"], "display_name": c["display_name"]}
                    for c in list_contacts(conn, account["id"])]
        try:
            hit = resolve_recipient(contacts, slots.get("contact", ""))
        except ChannelError as exc:
            if "ambiguous" in str(exc).lower():
                raise VoiceError(
                    f"That name matches several contacts on"
                    f" {account['label']}. Say the full name.")
            continue
        hits.append((account, hit))
    if not hits:
        raise VoiceError(
            f"I found no contact named {slots.get('contact', '').strip() or 'that'}"
            " on your active accounts. Add them in Channels first"
            " (contacts need their messaging ID).")
    if len(hits) > 1:
        options = ", ".join(f"{h[1]['display_name']} on {h[0]['label']}"
                            for h in hits[:4])
        raise VoiceError(f"That name matches several people: {options}."
                         " Say which one.")
    return hits[0]


def _when_utc(raw: str) -> str:
    try:
        return parse_time_to_utc(raw, "UTC")
    except ChannelError:
        raise VoiceError(
            f"I couldn't parse the time {raw.strip()!r}. Try"
            " something like 'at 11 PM'.") from None


def _start_message(conn, state: dict, owner_id: int, slots: dict,
                   transcript) -> dict:
    try:
        account, contact = _resolve_target(conn, owner_id, slots)
    except VoiceError as exc:
        return _reply(str(exc), transcript)
    when_raw = (slots.get("when") or "").strip()
    try:
        when_utc = _when_utc(when_raw) if when_raw else ""
    except VoiceError as exc:
        return _reply(str(exc), transcript)
    try:
        msg_id = draft_message(
            conn, account["id"], owner_id, contact_id=contact["id"],
            body=slots["body"])
    except ChannelError as exc:
        return _reply(f"I couldn't draft that: {exc}", transcript)
    state["pending"] = {"type": "message", "message_id": msg_id,
                        "when_utc": when_utc}
    summary = (f"Send to {contact['display_name']} on"
               f" {account['label']}: \u201c{slots['body'][:280]}\u201d")
    if when_utc:
        summary += (f", scheduled {when_utc} UTC (I assumed UTC;"
                    " name a timezone if you meant another).")
    else:
        summary += "."
    return _reply(summary + " Say yes to confirm, no to cancel.",
                  transcript, expect_confirmation=True, done=False)


def _confirm_message(conn, state: dict, owner_id: int,
                     transcript) -> dict:
    pending = state["pending"]
    state["pending"] = None
    try:
        if pending.get("when_utc"):
            schedule_message(conn, pending["message_id"], owner_id,
                             scheduled_for_utc=pending["when_utc"],
                             confirmed=True)
            return _reply(
                f"Scheduled for {pending['when_utc']} UTC. It sends"
                " when the scheduler runs.", transcript)
        outcome = send_now(conn, pending["message_id"], owner_id,
                           confirmed=True, post=None)
    except ChannelError as exc:
        return _reply(f"I couldn't send it: {exc}", transcript)
    if outcome["status"] == "sent":
        return _reply("Sent.", transcript)
    return _reply(f"The send failed: {outcome.get('error', 'unknown')}"
                  " The draft is kept; retry it from Channels.",
                  transcript)


def _cancel_pending(conn, state: dict, owner_id: int,
                    transcript) -> dict:
    pending = state["pending"]
    state["pending"] = None
    if pending and pending.get("type") == "message":
        try:
            cancel_message(conn, pending["message_id"], owner_id)
        except ChannelError:
            pass
        return _reply("Cancelled. The draft is discarded.",
                      transcript)
    if pending and pending.get("type") == "update_confirm":
        return _reply("Cancelled. No changes made.", transcript)
    if pending and pending.get("type") == "need_confirm":
        return _reply("Cancelled. The request was not posted.",
                      transcript)
    if pending and pending.get("type") == "pending_correct":
        return _reply("Cancelled. Nothing changed.", transcript)
    return _reply("Cancelled.", transcript)


_MISSING_QUESTIONS = {
    "contact": "Who should I send it to?",
    "channel": ("Which app: Telegram, WhatsApp, Messenger, or"
                " Instagram?"),
    "body": "What should the message say?",
    "title": "What should I call the project?",
}


def _collect(conn, state: dict, owner_id: int, kind: str,
             command: dict, transcript) -> dict:
    """Fill a multi-step command's missing slots from this turn."""
    pending = state["pending"]
    if command["kind"] in ("message", "project"):
        for key, value in command["slots"].items():
            if value:
                pending["slots"][key] = value
    elif command["kind"] == "unknown" and pending.get("missing"):
        need = pending["missing"][0]
        if need == "channel":
            word = channel_word(transcript.text)
            if word is None:
                return _reply(_MISSING_QUESTIONS["channel"],
                              transcript, done=False)
            pending["slots"]["channel"] = word
        else:
            pending["slots"][need] = transcript.text.strip()
    else:
        # A fresh command interrupts the half-built one.
        state["pending"] = None
        follow = handle_command(conn, state, owner_id,
                                state.get("twin_id"), command,
                                transcript)
        follow["text"] = ("Dropping that half-built request. "
                          + follow["text"])
        follow["speak"] = ("Dropping that half-built request. "
                           + follow["speak"])[:SPEAK_LIMIT]
        return follow
    required = ("contact", "channel", "body") if kind == "message" \
        else ("title",)
    pending["missing"] = [k for k in required
                          if not pending["slots"].get(k)]
    if pending["missing"]:
        return _reply(_MISSING_QUESTIONS[pending["missing"][0]],
                      transcript, done=False)
    state["pending"] = None
    if kind == "message":
        return _start_message(conn, state, owner_id,
                              pending["slots"], transcript)
    state["pending"] = {"type": "project",
                        "title": pending["slots"]["title"],
                        "goal": pending["slots"].get("goal", "")}
    return _reply(f"Create the project \u201c{pending['slots']['title'][:120]}\u201d?"
                  " Say yes to create it, no to cancel.",
                  transcript, expect_confirmation=True, done=False)


def _confirm_project(conn, state: dict, owner_id: int,
                     transcript) -> dict:
    pending = state["pending"]
    state["pending"] = None
    try:
        pid = create_project(conn, owner_id, pending["title"],
                             pending.get("goal", ""))
        log_event(conn, pid, "voice",
                  "created-from-voice", pending["title"][:200])
    except ProjectError as exc:
        return _reply(f"I couldn't create it: {exc}", transcript)
    return _reply(f"Created project #{pid}. Open Projects to break"
                  " it into tasks.", transcript)


def _status(conn, owner_id: int, transcript) -> dict:
    projects = list_projects(conn, owner_id)
    summary = usage_summary(conn, owner_id)
    cost = float(summary.get("est_cost_usd") or 0.0)
    return _reply(
        f"You have {len(projects)} project(s) and"
        f" {summary.get('requests', 0)} logged AI request(s) at"
        f" an estimated ${cost:.4f}. Voice itself is free.",
        transcript)


def _help(transcript) -> dict:
    return _reply(
        "You can create your twin ('create my twin'), update it"
        " ('add Python to my skills'), talk to it ('talk to my"
        " twin'), teach it ('teach ...'), find help ('find someone"
        " for web development'), post a request ('I need ...'),"
        " start a project ('build ...'), or send a message ('send"
        " a message to ...'). I always confirm before sending."
        " Say cancel to stop. No microphone? Type below instead.",
        transcript)


def _owner_name(conn, user_id: int) -> str:
    try:
        row = get_user(conn, user_id)
    except Exception:  # noqa: BLE001 - greeting nicety only
        return ""
    return (row["name"] if row else "") or ""


def _setup_branch(conn, state: dict, owner_id: int, command: dict,
                  transcript) -> dict:
    lowered = transcript.text.strip().lower().rstrip(".!?")
    if lowered in ("resume", "continue", "go on"):
        setup = state["setup"]
        state.pop("setup_paused", None)
        if setup.get("phase") == "review":
            return _reply(twin_setup.review_text(setup), transcript,
                          done=False)
        return _reply(twin_setup.current_question(setup), transcript,
                      done=False)
    if command["kind"] == "help":
        return _reply(
            "We're creating your twin, one question at a time. Say"
            " 'skip' to pass, 'back' to repeat, 'change <field>' to"
            " fix an answer, 'summary' to review, 'pause' to stop,"
            " or 'cancel' to drop the draft.",
            transcript, done=False)
    if command["kind"] in _SETUP_GUARD_KINDS:
        # "change/correct/edit <field>" is setup navigation even
        # though the parser labels it an update; true update ops
        # ("add/remove/set/clear ... my skills") stay blocked.
        if command["kind"] == "update" and re.search(
                r"\b(change|correct|edit|fix|redo)\b",
                transcript.text, re.IGNORECASE) and \
                "my twin" not in transcript.text.lower():
            result = twin_setup.setup_turn(state["setup"],
                                           transcript.text)
            if not result["done"]:
                return _reply(result["reply"], transcript,
                              done=False)
        setup = state["setup"]
        if setup.get("phase") == "review":
            next_step = "Say 'approve', 'edit <field>', or 'summary'."
        else:
            next_step = twin_setup.current_question(setup)
        return _reply(
            "Let's finish your twin first — or say 'cancel setup'"
            f" to drop the draft. {next_step}",
            transcript, done=False)
    result = twin_setup.setup_turn(state["setup"], transcript.text)
    if result.get("paused"):
        state["setup_paused"] = True
        return _reply(result["reply"], transcript, done=False)
    if not result["done"]:
        return _reply(result["reply"], transcript, done=False)
    setup = state["setup"]
    state["setup"] = None
    state.pop("setup_paused", None)
    if result.get("cancelled"):
        audit(conn, None, "voice.setup_cancelled", owner_id, "")
        return _reply(result["reply"], transcript)
    try:
        report = twin_setup.activate(
            conn, owner_id, _owner_name(conn, owner_id), setup)
    except ValueError as exc:
        return _reply(str(exc), transcript)
    return _reply(
        f"Your twin \u201c{report['display_name']}\u201d is active"
        f" and private ({report['filled']} of {report['total']}"
        " questions answered). Say 'talk to my twin' to try it, or"
        " 'teach my twin ...' to add knowledge.",
        transcript)


def _do_update(conn, state: dict, owner_id: int, twin_id: int | None,
               text: str, transcript) -> dict:
    if twin_id is None:
        return _reply("You don't have a twin yet. Say 'create my"
                      " twin' first.", transcript)
    parsed = twin_update_nl.parse_update(text)
    if not parsed["ops"]:
        return _prompt_update_field(state, transcript, text,
                                    parsed)
    return _run_update_ops(conn, state, owner_id, twin_id, parsed,
                           transcript)


def _prompt_update_field(state, transcript, text, parsed) -> dict:
    bare = re.match(
        r"^(update|change|edit)\s+my\s+(.+?)[\s.!?]*$", text,
        re.IGNORECASE)
    field = (twin_update_nl.field_from_words(bare.group(2))
             if bare else None)
    if field is not None:
        state["pending"] = {"type": "update_collect",
                            "field": field}
        examples = ("add Go" if
                    twin_update_nl.FIELDS[field][0] == "list"
                    else "set it to ...")
        return _reply(
            f"What should I change about your {field}? Say"
            f" '{examples}', or 'cancel'.", transcript,
            done=False)
    if parsed["errors"]:
        return _reply(" ".join(parsed["errors"]), transcript)
    return _reply(
        "What should I change? For example: 'add Python to my"
        " skills', 'set my headline to ...', or 'remove X from"
        " my interests'.", transcript, done=False)


def _run_update_ops(conn, state: dict, owner_id: int,
                    twin_id: int | None, parsed: dict,
                    transcript) -> dict:
    try:
        preview = twin_update_nl.preview(conn, twin_id, parsed["ops"])
    except ValueError as exc:
        return _reply(str(exc), transcript)
    if preview["destructive"]:
        state["pending"] = {"type": "update_confirm",
                            "ops": parsed["ops"]}
        lines = "; ".join(
            f"{c['field']}: {c['before']} → {c['after']}"
            for c in preview["changes"])
        return _reply(
            f"This will empty data: {lines}. Say yes to confirm,"
            " no to cancel.", transcript, expect_confirmation=True,
            done=False)
    if not preview["changes"]:
        return _reply(" ".join(parsed["errors"]
                               or preview["errors"]
                               or ["No changes."]), transcript)
    try:
        report = twin_update_nl.apply(conn, owner_id, twin_id,
                                      parsed["ops"])
    except ValueError as exc:
        return _reply(str(exc), transcript)
    state["last_undo"] = {"twin_id": twin_id,
                          "snap": report["undone"]}
    return _reply(twin_update_nl.describe(report), transcript)


def _consume_update_collect(conn, state: dict, owner_id: int,
                              twin_id: int | None, command: dict,
                              transcript) -> dict:
    field = (state.get("pending") or {}).get("field")
    text = transcript.text
    if re.match(r"^(actually\s+)?(nothing|never\s*mind|forget\s*it"
                r"|nope)\b", text.strip(), re.IGNORECASE):
        state["pending"] = None
        return _reply("Cancelled. No changes made.", transcript)
    parsed = twin_update_nl.parse_update(text, default_field=field)
    bare = text.strip(" .!?")
    if (not parsed["ops"] and bare
            and command["kind"] in ("unknown", "ask")
            and field is not None):
        # Bare text answering the prompt: set for text fields,
        # add for lists (never a silent delete).
        op = "add" if twin_update_nl.FIELDS[field][0] == "list" \
            else "set"
        values = [v.strip() for v in bare.split(",") if v.strip()] \
            if op == "add" else [bare]
        parsed = {"ops": [{"op": op, "field": field,
                           "values": values, "target": ""}],
                  "errors": []}
    if not parsed["ops"]:
        return _reply(
            " ".join(parsed["errors"])
            + f" (Still waiting on your {field}; say 'cancel' to"
            " stop.)", transcript, done=False)
    state["pending"] = None
    return _run_update_ops(conn, state, owner_id, twin_id, parsed,
                           transcript)


def _apply_pending_update(conn, state: dict, owner_id: int,
                          transcript) -> dict:
    ops = (state["pending"] or {}).get("ops", [])
    twin_id = state.get("twin_id")
    state["pending"] = None
    if twin_id is None:
        return _reply("You don't have a twin.", transcript)
    try:
        report = twin_update_nl.apply(conn, owner_id, twin_id, ops)
    except ValueError as exc:
        return _reply(str(exc), transcript)
    state["last_undo"] = {"twin_id": twin_id,
                          "snap": report["undone"]}
    return _reply(twin_update_nl.describe(report), transcript)


def _do_undo(conn, state: dict, owner_id: int, transcript) -> dict:
    saved = state.get("last_undo")
    if not saved:
        return _reply("Nothing to undo.", transcript)
    try:
        twin_update_nl.undo(conn, owner_id, saved["twin_id"],
                            saved["snap"])
    except ValueError as exc:
        return _reply(str(exc), transcript)
    state["last_undo"] = None
    return _reply("Undone. Your twin is back to how it was.",
                  transcript)


def _chat_enter(conn, state: dict, twin_id: int | None,
                transcript) -> dict:
    if twin_id is None:
        return _reply("You don't have a twin yet. Say 'create my"
                      " twin' first.", transcript)
    state["chat"] = True
    state["chat_history"] = []
    state["chat_sources"] = []
    twin = get_twin(conn, twin_id)
    name = twin["display_name"] if twin else "your twin"
    return _reply(f"Sure. {name} is ready. What would you like to"
                  " ask? (Say 'stop' any time to leave.)",
                  transcript, done=False)


def _chat_exit_text() -> str:
    return "Leaving our chat. Say 'talk to my twin' any time."


def _chat_branch(conn, state: dict, owner_id: int,
                 twin_id: int | None, command: dict,
                 transcript) -> dict:
    text = transcript.text.strip()
    kind = command["kind"]
    if _CHAT_EXIT.match(text) or kind == "cancel":
        state["chat"] = False
        return _reply(_chat_exit_text(), transcript)
    corrected = _CHAT_CORRECT.match(text)
    if corrected and corrected.group(2).strip():
        return _start_correction(
            conn, state, owner_id, twin_id,
            corrected.group(2).strip(), transcript)
    pending = state.get("pending")
    if pending is not None and is_affirm(transcript.text):
        return _apply_pending_correct(conn, state, owner_id,
                                      transcript)
    if pending is not None and is_deny(transcript.text):
        return _cancel_pending(conn, state, owner_id, transcript)
    if kind == "help":
        return _reply(
            "You're talking to your twin. Ask anything it should"
            " know, say 'what do you remember', 'teach ...' to add"
            " knowledge, 'correct ...' to fix an answer, or 'stop'"
            " to leave.", transcript, done=False)
    if kind == "status":
        return _status(conn, owner_id, transcript)
    if kind == "undo":
        return _do_undo(conn, state, owner_id, transcript)
    if kind in _CHAT_INTERRUPT_KINDS:
        state["chat"] = False
        follow = handle_command(conn, state, owner_id, twin_id,
                                command, transcript)
        follow["text"] = _chat_exit_text() + " " + follow["text"]
        follow["speak"] = (_chat_exit_text() + " "
                           + follow["speak"])[:SPEAK_LIMIT]
        return follow
    return _chat_turn(conn, state, owner_id, twin_id, command,
                      transcript)


def _chat_keywords(text: str) -> str:
    # Same length floor as memories._tokens so topic words survive.
    words = [w.strip(" .?,!").lower() for w in (text or "").split()]
    return " ".join(w for w in words if len(w) > 2)[:120]


def _chat_turn(conn, state: dict, owner_id: int,
               twin_id: int | None, command: dict,
               transcript) -> dict:
    if twin_id is None:
        state["chat"] = False
        return _reply("Your twin is gone. " + _chat_exit_text(),
                      transcript)
    kind = command["kind"]
    if kind == "teach":
        return _do_teach(conn, state, owner_id, twin_id,
                         command["slots"].get("fact", ""),
                         transcript)
    if kind == "remember":
        return _do_remember(conn, state, owner_id, twin_id,
                            command["slots"], transcript)
    if kind in ("approve_mem", "reject_mem"):
        return _do_mem_moderate(
            conn, state, owner_id, twin_id,
            command["slots"].get("n"),
            kind == "approve_mem", transcript)
    raw = command.get("raw") or transcript.text
    question = command["slots"].get("question", raw) if kind == "ask" \
        else raw
    out = twin_mind.answer_question(conn, twin_id, question)
    if (not out["sources"] and state.get("chat_history")
            and len(question.split()) <= 8):
        last_q = state["chat_history"][-1][0]
        retry = twin_mind.answer_question(
            conn, twin_id, f"{question} {_chat_keywords(last_q)}")
        if retry["sources"]:
            out = dict(retry)
            out["answer"] = ("(Following our topic.) "
                             + retry["answer"])
    state["chat_history"].append((question, out["answer"][:300]))
    del state["chat_history"][:-CHAT_HISTORY]
    state["chat_sources"] = out["sources"]
    return _reply(out["answer"], transcript,
                  speak=out["answer"][:SPEAK_LIMIT], done=False)


def _start_correction(conn, state: dict, owner_id: int,
                      twin_id: int, new_fact: str,
                      transcript) -> dict:
    sources = state.get("chat_sources") or []
    if not sources:
        return _reply(
            "There's no recent cited answer to correct. Tell me the"
            " fact with 'teach ...' instead.", transcript,
            done=False)
    old_id = sources[-1].get("id")
    old = get_memory(conn, old_id) if old_id else None
    if old is None or old["twin_id"] != twin_id:
        return _reply("I couldn't find that memory any more.",
                      transcript, done=False)
    state["pending"] = {"type": "pending_correct", "old_id": old_id,
                        "new": new_fact[:2000]}
    return _reply(
        f"Replace \u201c{old['content'][:200]}\u201d with"
        f" \u201c{new_fact[:200]}\u201d? Say yes to confirm.",
        transcript, expect_confirmation=True, done=False)


def _apply_pending_correct(conn, state: dict, owner_id: int,
                           transcript) -> dict:
    pending = state["pending"] or {}
    twin_id = state.get("twin_id")
    state["pending"] = None
    old = get_memory(conn, pending.get("old_id", -1))
    if twin_id is None or old is None or old["twin_id"] != twin_id:
        return _reply("That memory is gone; nothing changed.",
                      transcript)
    try:
        set_status(conn, old["id"], "rejected")
        new_id = create_memory(
            conn, twin_id=twin_id, content=pending.get("new", ""),
            source="voice:correction", status="pending",
            category=old["category"] or "general")
    except MemoryError as exc:
        return _reply(f"I couldn't save that: {exc}", transcript)
    audit(conn, None, "voice.memory_corrected", owner_id,
          f"old={old['id']} new={new_id}")
    return _reply("Done. The old answer is retired and the"
                  " correction is pending your approval.",
                  transcript)


def _do_teach(conn, state: dict, owner_id: int, twin_id: int | None,
              fact: str, transcript) -> dict:
    if twin_id is None:
        return _reply("You don't have a twin yet. Say 'create my"
                      " twin' first.", transcript)
    cleaned = " ".join((fact or "").split())
    if not cleaned:
        state["pending_teach"] = True
        return _reply("What should your twin remember?",
                      transcript, done=False)
    state["pending_teach"] = False
    try:
        mem_id = create_memory(
            conn, twin_id=twin_id, content=cleaned[:4000],
            source=f"{transcript.provider}:teach",
            status="pending", category=classify(cleaned))
    except MemoryError as exc:
        return _reply(f"I couldn't save that: {exc}", transcript)
    audit(conn, None, "voice.memory_taught", owner_id,
          f"mem={mem_id}")
    return _reply(
        f"Saved as pending knowledge: \u201c{cleaned[:200]}\u201d."
        " Say 'approve latest' to activate it, or review it under"
        " Memories.", transcript, done=bool(state.get("chat")))


def _do_remember(conn, state: dict, owner_id: int,
                 twin_id: int | None, slots: dict,
                 transcript) -> dict:
    if twin_id is None:
        return _reply("You don't have a twin yet. Say 'create my"
                      " twin' first.", transcript)
    topic = (slots.get("topic") or "").strip()
    pending_only = bool(slots.get("pending_only"))
    if topic:
        texts = relevant_memories(conn, twin_id, topic, 10)
        rows = {r["content"]: r
                for r in list_memories(conn, twin_id, "approved")}
        matched = [rows[t] for t in texts if t in rows]
        if not matched:
            return _reply(f"Your twin remembers nothing about"
                          f" {topic[:100]} yet. Teach it with"
                          " 'teach ...'.", transcript,
                          done=bool(state.get("chat")))
        state["mem_list"] = [m["id"] for m in matched]
        lines = [f"{i + 1}. {m['content'][:160]}"
                 for i, m in enumerate(matched)]
        return _reply(f"About {topic[:100]}:\n" + "\n".join(lines),
                      transcript, done=bool(state.get("chat")))
    approved = list_memories(conn, twin_id, "approved")
    pending = list_memories(conn, twin_id, "pending")
    if pending_only:
        if not pending:
            return _reply("No pending knowledge. Everything taught"
                          " is decided.", transcript,
                          done=bool(state.get("chat")))
        state["mem_list"] = [m["id"] for m in pending[:10]]
        lines = [f"{i + 1}. {m['content'][:160]} (pending)"
                 for i, m in enumerate(pending[:10])]
        return _reply("Pending knowledge:\n" + "\n".join(lines)
                      + "\nSay 'approve memory <number>'.",
                      transcript, done=bool(state.get("chat")))
    if not approved and not pending:
        return _reply("Your twin remembers nothing yet. Teach it"
                      " with 'teach ...' or import a chat export.",
                      transcript, done=bool(state.get("chat")))
    shown = approved[:10]
    state["mem_list"] = [m["id"] for m in shown]
    lines = [f"{i + 1}. {m['content'][:160]}"
             for i, m in enumerate(shown)]
    text = f"Your twin remembers {len(approved)} thing(s):\n" \
        + "\n".join(lines)
    if pending:
        text += (f"\nPlus {len(pending)} pending — say 'show"
                 " pending' to review.")
    speak = f"Your twin remembers {len(approved)} things."
    if shown:
        speak += f" First: {shown[0]['content'][:200]}"
    return _reply(text, transcript, speak=speak,
                  done=bool(state.get("chat")))


def _resolve_mem_number(conn, state: dict, twin_id: int,
                        number) -> dict | None:
    if number == "latest" or number is None:
        pending = list_memories(conn, twin_id, "pending")
        return dict(pending[-1]) if pending else None
    try:
        idx = int(number) - 1
    except (TypeError, ValueError):
        return None
    ids = state.get("mem_list") or []
    if idx < 0 or idx >= len(ids):
        return None
    row = get_memory(conn, ids[idx])
    if row is None or row["twin_id"] != twin_id:
        return None
    return dict(row)


def _do_mem_moderate(conn, state: dict, owner_id: int,
                     twin_id: int | None, number,
                     approve: bool, transcript) -> dict:
    if twin_id is None:
        return _reply("You don't have a twin yet.", transcript)
    mem = _resolve_mem_number(conn, state, twin_id, number)
    if mem is None:
        return _reply("Which one? Say 'show pending' to list them"
                      " by number first.", transcript,
                      done=bool(state.get("chat")))
    if approve and mem["status"] == "approved":
        return _reply("That one is already approved.", transcript,
                      done=bool(state.get("chat")))
    try:
        if approve:
            set_status(conn, mem["id"], "approved")
            audit(conn, None, "voice.memory_approved", owner_id,
                  f"mem={mem['id']}")
            text = "Approved. Your twin will use it from now on."
        else:
            delete_memory(conn, mem["id"])
            audit(conn, None, "voice.memory_deleted", owner_id,
                  f"mem={mem['id']}")
            text = "Removed. Your twin will no longer use it."
    except MemoryError as exc:
        return _reply(f"I couldn't do that: {exc}", transcript)
    return _reply(text, transcript, done=bool(state.get("chat")))


def _do_find(conn, state: dict, twin_id: int | None, slots: dict,
             transcript) -> dict:
    if twin_id is None:
        return _reply("You need a twin first so I know your"
                      " skills. Say 'create my twin'.",
                      transcript)
    twin = get_twin(conn, twin_id)
    if twin is None:
        return _reply("Your twin is gone.", transcript)
    skills = slots.get("skills") or []
    try:
        matches = find_matches(conn, twin,
                               skills_filter=skills or None,
                               limit=5)
    except ValueError as exc:
        return _reply(f"Search failed: {exc}", transcript)
    if not matches:
        hint = f" for {', '.join(skills)}" if skills else ""
        return _reply(f"No open opportunities{hint} right now.",
                      transcript)
    lines = []
    for item in matches[:3]:
        opp, owner, match = (item["opportunity"], item["owner"],
                             item["match"])
        who = owner["twin_display_name"] or owner["name"]
        lines.append(f"#{opp['id']} {opp['title']} ({opp['kind']},"
                     f" score {match.score}) by {who}. Needs:"
                     f" {', '.join(opp['required_skills'][:4]) or '—'}.")
    text = "\n".join(lines)
    return _reply(text, transcript, speak=matches[0]["opportunity"]
                  ["title"][:200])


def _do_opportunities(conn, state: dict, twin_id: int | None,
                      slots: dict, transcript) -> dict:
    if twin_id is not None:
        twin = get_twin(conn, twin_id)
        matches = find_matches(
            conn, twin, skills_filter=slots.get("skills") or None,
            limit=5) if twin is not None else []
        if not matches:
            return _reply("No open opportunities match right now.",
                          transcript)
        lines = [f"#{m['opportunity']['id']}"
                 f" {m['opportunity']['title']}"
                 f" ({m['opportunity']['kind']}, score"
                 f" {m['match'].score})" for m in matches]
        return _reply(f"Top matches:\n" + "\n".join(lines),
                      transcript)
    rows = list_opportunities(conn, status="open", limit=5)
    if not rows:
        return _reply("No open opportunities right now.",
                      transcript)
    lines = [f"#{r['id']} {r['title']} ({r['kind']})" for r in rows]
    return _reply(f"Latest open opportunities:\n" + "\n".join(lines),
                  transcript)


def _do_need(conn, state: dict, owner_id: int, twin_id: int | None,
             text: str, transcript) -> dict:
    if twin_id is None:
        return _reply("You need a twin first. Say 'create my"
                      " twin'.", transcript)
    parsed = parse_need(text)
    reqs = parsed.get("requirements", {}) or {}
    bits = [f"{k}={v}" for k, v in list(reqs.items())[:4]
            if v not in (None, "", [], {})]
    state["pending"] = {"type": "need_confirm",
                        "twin_id": twin_id,
                        "category": parsed.get("category", "other"),
                        "need_text": text[:2000],
                        "requirements": reqs}
    proposal = (f"Post this request? Category:"
                f" {parsed.get('category', 'other')}"
                + (f"; {', '.join(bits)}" if bits else "")
                + ". Say yes to post, no to cancel.")
    for warning in (parsed.get("warnings") or [])[:2]:
        proposal += f" Note: {warning}"
    return _reply(proposal, transcript, expect_confirmation=True,
                  done=False)


def _post_pending_need(conn, state: dict, owner_id: int,
                       transcript) -> dict:
    pending = state["pending"] or {}
    state["pending"] = None
    try:
        req_id, _created = need_engine.create_need(
            conn, twin_id=pending.get("twin_id"),
            owner_user_id=owner_id,
            category=pending.get("category", "other"),
            need_text=pending.get("need_text", ""),
            requirements=pending.get("requirements"))
        need_engine.confirm_request(conn, req_id, owner_id)
    except Exception as exc:  # noqa: BLE001 - engine error to user
        return _reply(f"I couldn't post it: {exc}", transcript)
    return _reply(f"Posted request #{req_id}"
                  f" ({pending.get('category', 'other')}).",
                  transcript)


def _do_my_chats(conn, owner_id: int, transcript) -> dict:
    rows = list_for_user(conn, owner_id)
    if not rows:
        return _reply("You have no twin chats yet.", transcript)
    lines = []
    for row in rows[:5]:
        lines.append(f"#{row['id']} {row['name_a']} /"
                     f" {row['name_b']}: {row['status']}")
    text = f"You have {len(rows)} chat(s):\n" + "\n".join(lines)
    return _reply(text, transcript)


def handle_command(conn, state: dict, owner_id: int,
                   twin_id: int | None, command: dict,
                   transcript) -> dict:
    kind = command["kind"]
    if kind == "cancel":
        if state["pending"] is None:
            return _reply("Nothing to cancel.", transcript)
        return _cancel_pending(conn, state, owner_id, transcript)
    if kind in ("message", "project") and command.get("missing"):
        state["pending"] = {"type": f"{kind}_collect",
                            "slots": dict(command["slots"]),
                            "missing": list(command["missing"])}
        return _reply(_MISSING_QUESTIONS[command["missing"][0]],
                      transcript, done=False)
    if kind == "message":
        return _start_message(conn, state, owner_id,
                              command["slots"], transcript)
    if kind == "project":
        state["pending"] = {"type": "project",
                            "title": command["slots"]["title"],
                            "goal": command["slots"].get("goal", "")}
        title = command["slots"]["title"][:120]
        return _reply(f"Create the project \u201c{title}\u201d? Say yes"
                      " to create it, no to cancel.",
                      transcript, expect_confirmation=True, done=False)
    if kind == "ask":
        if twin_id is None:
            return _reply("Set up your twin first; I answer from"
                          " its approved memories.", transcript)
        out = twin_mind.answer_question(
            conn, twin_id, command["slots"]["question"])
        return _reply(out["answer"], transcript,
                      speak=out["answer"][:SPEAK_LIMIT])
    if kind == "status":
        return _status(conn, owner_id, transcript)
    if kind == "help":
        return _help(transcript)
    if kind == "setup":
        if twin_id is not None:
            return _reply("You already have a twin. Say 'update my"
                          " twin' to change it, or 'talk to my twin'"
                          " to chat.", transcript)
        if state.get("setup") is not None:
            setup = state["setup"]
            if setup.get("phase") == "review":
                return _reply(twin_setup.review_text(setup),
                              transcript, done=False)
            return _reply(twin_setup.current_question(setup),
                          transcript, done=False)
        state["setup"] = twin_setup.new_setup()
        return _reply(
            twin_setup.start_text(_owner_name(conn, owner_id)),
            transcript, done=False)
    if kind == "update":
        return _do_update(conn, state, owner_id, twin_id,
                          command["slots"].get("text", ""), transcript)
    if kind == "undo":
        return _do_undo(conn, state, owner_id, transcript)
    if kind == "chat":
        return _chat_enter(conn, state, twin_id, transcript)
    if kind == "teach":
        return _do_teach(conn, state, owner_id, twin_id,
                         command["slots"].get("fact", ""),
                         transcript)
    if kind == "remember":
        return _do_remember(conn, state, owner_id, twin_id,
                            command["slots"], transcript)
    if kind in ("approve_mem", "reject_mem"):
        return _do_mem_moderate(
            conn, state, owner_id, twin_id,
            command["slots"].get("n"),
            kind == "approve_mem", transcript)
    if kind == "find":
        return _do_find(conn, state, twin_id, command["slots"],
                        transcript)
    if kind == "opportunities":
        return _do_opportunities(conn, state, twin_id,
                                 command["slots"], transcript)
    if kind == "need":
        return _do_need(conn, state, owner_id, twin_id,
                        command["slots"].get("text", ""), transcript)
    if kind == "my_chats":
        return _do_my_chats(conn, owner_id, transcript)
    return _reply("I didn't catch that as a command. Say 'help'"
                  " to hear what I can do.", transcript)


def handle_transcript(conn, *, user_id: int, twin_id: int | None,
                      text: str, provider: str = "browser") -> dict:
    """Run one voice/text turn. Raises VoiceError only for provider
    problems (empty transcript, unconfigured Meta); everything else
    becomes a spoken-style reply."""
    transcript = get_provider(provider).accept_transcript(text)
    state = _state(user_id)
    state["twin_id"] = twin_id
    command = parse_command(transcript.text)
    audit(conn, None, "voice.command", user_id,
          f"{transcript.provider}:{command['kind']}:"
          f"{transcript.text[:160]}")
    _history(state, command["kind"], transcript.text)
    if state.get("setup") is not None:
        return _setup_branch(conn, state, user_id, command,
                             transcript)
    if state.pop("pending_teach", False):
        if command["kind"] == "cancel":
            return _reply("Cancelled. Nothing was saved.",
                          transcript)
        if command["kind"] in ("unknown", "ask"):
            return _do_teach(conn, state, user_id, twin_id,
                             transcript.text, transcript)
        # Another command supersedes the teach prompt.
    if state.get("chat"):
        return _chat_branch(conn, state, user_id, twin_id, command,
                            transcript)
    pending = state["pending"]
    if pending is not None and is_affirm(transcript.text):
        if pending.get("type") == "message":
            return _confirm_message(conn, state, user_id, transcript)
        if pending.get("type") == "project":
            return _confirm_project(conn, state, user_id, transcript)
        if pending.get("type") == "update_confirm":
            return _apply_pending_update(conn, state, user_id,
                                         transcript)
        if pending.get("type") == "need_confirm":
            return _post_pending_need(conn, state, user_id,
                                      transcript)
        if pending.get("type") == "pending_correct":
            return _apply_pending_correct(conn, state, user_id,
                                          transcript)
        if pending.get("type") == "update_collect":
            return _reply(
                f"Tell me the change for your {pending['field']},"
                " or say 'cancel'.", transcript, done=False)
        # "Yes" while collecting slots: re-ask, don't guess.
        return _reply(_MISSING_QUESTIONS[pending["missing"][0]],
                      transcript, done=False)
    if pending is not None and is_deny(transcript.text):
        return _cancel_pending(conn, state, user_id, transcript)
    if pending is not None and pending.get("type") in (
            "message_collect", "project_collect"):
        return _collect(conn, state, user_id, pending["type"][:-8],
                        command, transcript)
    if (pending is not None
            and pending.get("type") == "update_collect"
            and command["kind"] in ("unknown", "ask", "update")):
        return _consume_update_collect(conn, state, user_id,
                                       twin_id, command,
                                       transcript)
    if pending is not None:
        # A new command interrupts an unconfirmed action.
        state["pending"] = None
        if pending.get("type") == "message":
            try:
                cancel_message(conn, pending["message_id"], user_id)
            except ChannelError:
                pass
        follow = handle_command(conn, state, user_id, twin_id,
                                command, transcript)
        follow["text"] = ("Dropping the unconfirmed request. "
                          + follow["text"])
        follow["speak"] = ("Dropping the unconfirmed request. "
                           + follow["speak"])[:SPEAK_LIMIT]
        return follow
    return handle_command(conn, state, user_id, twin_id, command,
                          transcript)
