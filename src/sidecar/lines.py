"""the deer's authored voice: small, quiet, and written ahead of time.

the sidecar makes no model calls (no keys ever live on the device), so
every word the deer says here was authored - the velvet antler principle:
authored lines first, generated language later as a server-batched pool.
lines are deliberately small; the deer is presence, not commentary, and
during a run she must never become another task.

voice notes (docs/COUNCIL_VOICE_REFERENCE.md, vel-adjacent but quieter):
lowercase, companionable, zero coach pressure. pausing carries NO
disappointment - stopping is information the bank keeps. the block target
crossing is a gentle ding, never an ending. finishing a TASK is the big
moment: full emergency unloaf, confetti-sized joy.
"""
from __future__ import annotations

import random
from typing import Optional

LINE_POOLS: dict[str, list[str]] = {
    "focus_start": [
        "okay. i'm right here. 🦌 *settles into a loaf*",
        "clock's running. you work, i'll loaf.",
        "*loafs companionably* go on then. i've got the watch.",
        "on it. one clock, one task, one deer.",
        "here we go. i'll be pleasantly irrelevant until further notice.",
        "*tucks legs in* i'm set. your move.",
    ],
    "focus_switch": [
        "*ears swivel* new heading. old minutes are banked, not lost.",
        "switching. the other clock keeps everything you gave it.",
        "okay - this one now. i'll watch this lane instead.",
        "noted. previous task tucked in safe, new clock running.",
    ],
    "focus_pause": [
        "paused. the minutes are banked. stretch something?",
        "clock's off. everything you did still counts.",
        "*unhurried* we pause when pausing's right. i'm still here.",
        "resting the clock. no notes, no verdict.",
        "paused and held. come back whenever.",
    ],
    "block_target": [
        "*soft ding* 🦌 that's a whole block. land it, or keep rolling - "
        "i'm counting either way.",
        "*lifts head* block's full. everything past here is bonus minutes.",
        "that's the block! no need to stop - the clock just counts extra now.",
        "*approving ear flick* target reached. overtime is yours if you "
        "want it.",
    ],
    "focus_hold": [
        "clock's stopped. one small question on the card whenever "
        "you're ready - no rush.",
        "*holds the clock gently* stopped, not banked - there's a "
        "little question waiting first.",
        "paused the counting. the card has one question, and it can "
        "wait as long as you like.",
    ],
    "drift_checkin": [
        "*ears soft* you've been quiet a bit - still with me? no pressure, "
        "blocks wait.",
        "hey, gently: the clock's still running. break or lost the thread? "
        "either is fine.",
        "*lifts head, unhurried* just noticing you wandered. i'll be here "
        "whenever you land back.",
        "soft check-in: still on this one? if you're resting, rest - i've "
        "got the clock.",
    ],
    "drift_return": [
        "*ears perk* welcome back. the clock never judged.",
        "there you are. picking up right where you left it.",
        "back!! okay. *settles again* we simply continue.",
        "returned and accounted for. the block missed you, quietly.",
    ],
    # the stuck run's two moments (docs/STUCK_MODE_DESIGN.md section 4):
    # the boundary replaces the block_target ding - two exits, equal
    # weight, never a nudge to continue - and the stop at the line gets
    # its own celebration. stopping on purpose is the whole skill.
    "stuck_boundary": [
        "*soft ding* that's the thing. stop here, or keep going - both count.",
        "*lifts head* you did the piece. stopping now is a whole choice. so "
        "is going on.",
        "there's the line you drew. stand on it or step over it - i'm "
        "counting either way.",
        "*ears up, gently* the container's full. stop here is a real ending. "
        "keep going is a real choice.",
    ],
    "stuck_stopped": [
        "stopped at the line you drew. that's the whole skill.",
        "*settles* you said a few minutes and meant it. banked.",
        "ended on purpose. that counts for more than it looks.",
        "*approving ear flick* a boundary, kept. the minutes are in the bank.",
    ],
    "task_finished": [
        "*FULL EMERGENCY UNLOAF* DONE!! you FINISHED it!! 🦌🎉 look at "
        "that - an actual completed thing, witnessed by an actual deer!!",
        "IT'S DONE!!! *prances in a small circle* 🎉🦌 that task is "
        "FINISHED and nobody can untask it!!",
        "*ears shoot ALL the way up* FINISHED!! 🎉 that one's off the "
        "list forever. i'm so proud of you!!",
        "DONE DONE DONE!! 🦌✨🎉 *celebratory hoof stomp* you took a "
        "whole task out of the world's inbox!!",
        "*unloafs at maximum velocity* COMPLETE!! 🎉 write it in the "
        "ledger, tell edwin, tell EVERYONE!!",
    ],
}


def pick_line(moment: str, last: Optional[str] = None) -> str:
    """a random authored line for this moment, never the same one twice in a
    row (the deer repeating herself verbatim breaks the being-alive spell)."""
    pool = LINE_POOLS.get(moment)
    if not pool:
        return ""
    candidates = [line for line in pool if line != last] or pool
    return random.choice(candidates)
