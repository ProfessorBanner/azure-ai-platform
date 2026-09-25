# aks-mlops capability — G3 cost controls (no resources deployed)

Two independent roots for the Pet Image Classifier GPU lab:

| Root | Lifecycle | State key |
|---|---|---|
| `controls/` | control + retained + the EMPTY disposable session group; expiry watchdog (Azure Automation), custom delete-only role, retained storage, optional budget | `capabilities/aks-mlops/controls.tfstate` |
| `session/` | disposable AKS Free lab (1 system node, optional single T4 node disabled by default) + Basic ACR, deployed INTO the session group | `capabilities/aks-mlops/session.tfstate` |

Design, price/quota evidence, admission rules, the G4 staged order and the
live-verification checklist: `products/pet-classifier/docs/g3-cost-controls.md`
and `docs/adr/0012-aks-mlops-cost-controls.md`.

Nothing here has been applied. Plans are produced read-only; `terraform
apply` is a human-approved G4 action.
