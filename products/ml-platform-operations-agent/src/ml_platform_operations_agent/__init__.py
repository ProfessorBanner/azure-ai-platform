"""Read-only, evidence-bound diagnosis of ML model degradation.

Bounded question: "Why did model X degrade during the last seven days?"

The agent inspects evidence and recommends investigation. It performs no
state-changing ML operation of any kind — there is no retrain, promote, alias,
job or SQL capability in this package, so the claim rests on absent code rather
than on a policy that merely declines.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
