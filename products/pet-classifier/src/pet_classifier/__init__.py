"""Local-first pet breed image classifier (G1).

G1 is entirely local: dataset preparation, CPU training of a linear head on a
frozen ImageNet ResNet-18 backbone, evaluation, MLflow tracking, a versioned
model package, a prediction API and a small web UI. No cloud resources.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
