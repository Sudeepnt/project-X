"""The assistant: say what you want in plain words; it works out what you mean and does it.

The design follows Reflexion (Shinn et al., NeurIPS 2023):

- Actor: the model turns a message into a structured plan (schedule, reschedule, cancel, find free
  time, remind, show the agenda), and says in one sentence what it understood.
- Evaluator: plain code checks the plan against the real state (the date is real and ahead, the
  event exists, the slot is free, the person resolves to one email). The paper's clearest result is
  that a grounded evaluator matters most, and that passing a wrong answer is worse than failing a
  right one, so the checks are strict.
- Self-reflection: when checks fail, the model gets its plan and the failures, writes what it
  misread, and tries again (up to three trials, climbing a model rung each time).
- Memory: the last few turns are short-term memory. When the person corrects it ("no, I meant the
  Friday after"), the lesson is stored and the three most relevant lessons go into every future
  prompt, the paper's bounded long-term memory.

Anything outward (inviting people) still waits on a decision card from the policy engine.
"""
