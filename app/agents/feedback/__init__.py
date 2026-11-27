"""Outcome feedback on AI executions (`answer_feedback`).

Owned by: agents/. Same ownership shape as `agents.knowledge_gap`: it owns one
table (`answer_feedback`) and reads the agents-owned `agent_executions`.

Three layers, deliberately kept distinct (the point of the whole design):

  1. RAW USER FEEDBACK     -- `service.submit_feedback`: one opinion per user
                              per execution. Not a verdict.
  2. AUTOMATED PATTERNS    -- `patterns.identify_patterns`: clusters of
                              negatively-rated questions, computed on read,
                              never persisted, always labelled unreviewed.
  3. REVIEWED EVAL CASES   -- `app.evaluation.feedback_candidates`: a human
                              names themselves as the reviewer in a
                              version-controlled file. The only layer allowed
                              to influence an evaluation dataset.

Nothing in 1 or 2 changes agent behaviour, thresholds or datasets.
"""
