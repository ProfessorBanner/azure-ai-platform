"""Deterministic offline evaluation for ml-platform-operations-agent.

No LLM judge, no network, no clock. Phase 19.2d wraps `scorers` for
`mlflow.genai.evaluate`; the gates are proven here first.
"""
