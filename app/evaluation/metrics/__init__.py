"""Pure, independently-tested scoring functions -- see each submodule.

None of these functions perform I/O or know about `EvaluationCase`/
`EvaluationResult`; the runner (`app.evaluation.runner`) is the only caller
that threads dataset/result shapes through to these plain signatures. Keeping
the boundary this way is what makes every metric here directly unit-testable
with bare lists/dicts, per this package's "measure, don't duplicate" design.
"""
