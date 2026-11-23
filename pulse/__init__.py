"""Pulse, taken from Hermes Conduit's notifier plugin (hermes-conduit-notifier v0.195.4; MIT License, Copyright (c)
2026 Hermes Conduit contributors, see LICENSE-CONDUIT in this folder).

Pulse is an assistant that speaks first: once a chat goes quiet it keeps what matters (what Hermes promised, what the
user said they would do, what they asked to be told about), checks on each, and brings it up at the right time as a
card, with a notification when it is worth one; the user's reactions teach it what to bring up. Off unless turned on.

Each module here is Conduit's, unchanged but for the lines its first comment lists. What needs an iPhone (the phone's
calendar and reminders, where the user is, shops on the way, the Shortcuts inbox) finds no phone store in this plugin
and stays off, as Conduit itself allows ("no phone store in this build"). pluk_notify wires Pulse into its hooks
(../hooks_pulse.py) and its Dashboard routes (../dashboard/plugin_api.py).
"""
