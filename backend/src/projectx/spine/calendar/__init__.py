"""Calendar and reminders: shared by every department module.

- Calendar: schedule, reschedule and cancel events, and find free time inside working hours.
  Invitations to people outside the business are emails with an .ics file and go through the
  policy engine (``spine.calendar.send_invite``), like every other outward action.
- Reminders: one table for anything worth remembering, from a meeting to a domain renewal. A
  person can add one, or hand the model a piece of text and let it decide what to store; code
  checks every date and lead time before anything is saved.
"""
