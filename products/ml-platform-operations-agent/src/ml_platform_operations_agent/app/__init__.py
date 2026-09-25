"""The Databricks App surface.

A THIN ADAPTER. It parses a Responses request, calls the existing controlled
agent, and renders the Diagnosis. No policy, no evidence rule and no tool
selection lives here — those stay in `policy.py`, `evidence.py` and `agent.py`,
and this package would be the wrong place to re-decide any of them.
"""
