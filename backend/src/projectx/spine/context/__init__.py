"""What the assistant knows, and the assistant staying on.

- ``store``: our own long-term memory. Preferences, people, facts, routines and promises, learned from
  conversations and kept with full history in PostgreSQL. No model provider holds any of it; each call
  gets only the few entries it needs.
- ``proactive``: the assistant wakes up on its own (a morning brief, checks before meetings, follow-ups
  on promises, clashes after a calendar sync), decides what is worth saying, and stays quiet otherwise.
"""
