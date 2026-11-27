"""Platform-operator surface: who may operate EKIP itself (not any one customer organization).

Deliberately separate from tenant roles and permissions: `tenancy:manage` and every other tenant
permission grant nothing here. Operators are rows in `platform_operators`, created only by the
owner-credential CLI (`scripts/platform_operator.py`).
"""
