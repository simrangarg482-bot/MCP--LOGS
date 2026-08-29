"""Pluggable "system under test" seams -- the only place `app.evaluation`
chooses between a deterministic fixture, a real EKIP interface, or an
optional live model call. See each submodule; `runner.py` selects which
adapter set to build from `EvaluationMode`.
"""
