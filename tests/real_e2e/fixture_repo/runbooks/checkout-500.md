# Checkout HTTP 500 runbook

When checkout returns HTTP 500, first inspect the payment adapter's
null-handling logs. If null-handling errors are confirmed, roll back the
checkout service to release `2026.08.16` and notify the Payments Reliability
team.
