# pet-classifier — G1: local CPU baseline

A small, understandable, working image classifier for the 37 breeds of the
Oxford-IIIT Pet dataset:

```
dataset → preprocessing → CPU training → evaluation → MLflow tracking
        → versioned model package → prediction API → web UI
```

G1 is **entirely local**. It creates no Azure resources, calls no paid APIs and
has no Docker, Kubernetes, Terraform or model registry. Later phases will take
the same product to GPU training and MLOps on AKS; G1 is the baseline they
build on and compare against.

## What it does

| Stage | Module | What happens |
|---|---|---|
| Prepare | `pet_classifier.data` | Downloads the dataset (explicit command only), reads the official annotation files, makes a deterministic stratified train/validation split of the official `trainval` split (seed 42, 20 % validation), leaves the official `test` split untouched, validates every image, drops unreadable and duplicate images, and writes `data/manifest.json`. |
| Train | `pet_classifier.train` | ResNet-18 with ImageNet weights, backbone frozen and kept in eval mode, a new 37-class linear head trained with cross-entropy on CPU. Every epoch is evaluated on the validation subset; the best epoch by macro-F1 is exported. Everything is logged to the local MLflow server. |
| Evaluate | `pet_classifier.evaluate` | Accuracy, macro-F1, per-class report and confusion matrix over *all* validation samples with the full 37-label list (`zero_division=0`), plus a training-set-majority baseline. |
| Package | `pet_classifier.artifacts` | Writes `artifacts/<run_id>/` atomically: CPU `state_dict`, metadata, metrics, class list, data manifest and the lockfile. Never overwrites an existing package. |
| Predict | `pet_classifier.predict` | `Predictor` loads one explicit package, verifies files, checksum, schema, architecture, class count and preprocessing, rebuilds ResNet-18 with `weights=None`, loads the `state_dict` with `weights_only=True`, and returns the top-3 model scores. Needs neither MLflow nor the network. |
| Serve | `pet_classifier.api` | FastAPI app: `GET /health/live`, `GET /health/ready`, `GET /` (upload form), `POST /predict` (HTML), `POST /api/v1/predict` (JSON). The Predictor is loaded once in the lifespan from an explicitly selected model directory. |

## Requirements

- Python 3.12 (the repository standard; see `pyproject.toml` for why not 3.11).
- [`uv`](https://docs.astral.sh/uv/).
- Network access **only** for `prepare` (dataset) and the first `train`
  (ImageNet weights, ~45 MB, cached by torchvision in `~/.cache/torch`).
- Disk: the dataset download is **~800 MB of archives** (`images.tar.gz`
  ~755 MB, `annotations.tar.gz` ~19 MB) and **~1.6 GB on disk** after
  extraction with the archives kept. The full archive is fetched even though a
  smoke run trains on 148 images.
- CPU only. A GPU is never selected automatically. `--device cuda` or
  `--device mps` is accepted only when named explicitly and available.

## Setup

All commands run from `products/pet-classifier/`.

```bash
cd products/pet-classifier
uv sync --frozen            # product-local .venv from the committed uv.lock
```

## Run

### 1. Prepare the dataset (downloads ~800 MB)

```bash
uv run python -m pet_classifier.data prepare --root data
```

Prints the split counts, the manifest hash and every excluded image with its
reason (`missing`, `unreadable`, `duplicate_content`). Re-running refuses to
overwrite `data/manifest.json` unless you pass `--force`; `--no-download`
rebuilds the manifest from files already on disk.

### 2. Start the MLflow tracking server

One SQLite backend, one absolute artifact directory, both inside the product
directory (and both git-ignored). Tested with MLflow 3.16.1:

```bash
uv run mlflow server \
  --host 127.0.0.1 --port 5000 \
  --backend-store-uri "sqlite:///$(pwd)/mlflow.db" \
  --artifacts-destination "$(pwd)/mlruns"
```

Training logs through `http://127.0.0.1:5000`, and artifacts are proxied
through the server into `mlruns/`. Stop and restart the server with the same
two paths and every experiment, run and artifact is still there.

macOS note: AirPlay Receiver also listens on port 5000. MLflow binds
`127.0.0.1:5000` specifically and takes precedence for loopback traffic, so
this works out of the box; if the bind ever fails, pick another port and pass
`--tracking-uri` / `PET_CLASSIFIER_MLFLOW_URL` accordingly.

### 3. Train (smoke profile)

```bash
uv run python -m pet_classifier.train --profile smoke --device cpu
```

Smoke profile: 4 training images and 2 validation images per class (selected
inside the already-separated splits), 1 epoch, batch size 8, `num_workers=0`.
It proves the pipeline is wired together; **it does not produce a useful
model**, and the reported accuracy is not evidence of one. `--profile full`
uses every sample for 5 epochs.

The run prints a JSON summary with the run ID, package path, elapsed seconds,
device, and validation and baseline metrics. The package lands in
`artifacts/<run_id>/`.

### 4. Serve the exported package

MLflow does not need to be running for this step.

```bash
export PET_CLASSIFIER_MODEL_DIR=artifacts/<run_id>
uv run uvicorn pet_classifier.api:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The page shows the active model version, whether
it is a smoke model, an upload form, and a link to the MLflow UI
(`PET_CLASSIFIER_MLFLOW_URL`, default `http://127.0.0.1:5000`).

JSON prediction:

```bash
curl -s -F "file=@some-pet.jpg" http://127.0.0.1:8000/api/v1/predict
```

```json
{
  "model_version": "<run_id>",
  "smoke": true,
  "predictions": [
    {"class_name": "Bengal", "score": 0.12},
    {"class_name": "Egyptian_Mau", "score": 0.09},
    {"class_name": "Abyssinian", "score": 0.07}
  ]
}
```

Restarting the app with the same `PET_CLASSIFIER_MODEL_DIR` serves exactly the
same package; nothing searches for a "latest" model.

## Test

Offline unit tests. They download nothing: images are generated in memory, the
fake dataset root mimics the official layout, and the model package comes from
a randomly initialised ResNet-18.

```bash
uv run pytest -q
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src tests
```

The real-data CPU smoke run (steps 1–3 above) is deliberately separate from
the unit tests.

## Model package contract

`artifacts/<run_id>/` (artifact schema `1.0`):

| File | Content |
|---|---|
| `model.pt` | `state_dict` of CPU tensors (parameters and buffers). Not a pickled model object. |
| `metadata.json` | schema version, MLflow run ID, architecture, number of classes, preprocessing definition, pretrained-weights identifier, dataset manifest hash, `model.pt` SHA-256, Git SHA and dirty flag, device, seed, profile, smoke flag, lockfile SHA-256. |
| `metrics.json` | best epoch, validation accuracy / macro-F1 / per-class report / confusion matrix, majority-class baseline, smoke flag. |
| `class_names.json` | the ordered class list; index = position. |
| `data_manifest.json` | the manifest the model was trained from. |
| `uv.lock` | the dependency lockfile at training time. |

The package is written to a temporary sibling directory and published with one
atomic rename, so a half-written package can never be loaded.

## Data contract

- Class labels come from the official `annotations/trainval.txt` and
  `annotations/test.txt`, not from private torchvision attributes.
- Class order is the sorted list of breed names, persisted in the manifest and
  in every package.
- The train/validation split is stratified by class, seeded (`42`), sorted
  before shuffling, and independent of filesystem order.
- The official `test` split is recorded in the manifest and never used for
  training or checkpoint selection.
- Validation checks: split disjointness, label range, class coverage in train
  and validation, image readability, and duplicate content across all kept
  images (the first occurrence in train → val → test order is kept, the rest
  are excluded with the surviving twin named).

## Reproducibility

Seeds are set for `random`, NumPy and PyTorch, the training loader uses a seeded
generator and `torch.use_deterministic_algorithms(True, warn_only=True)` is on.
This is best effort: the same seed on the same machine with the same library
versions should reproduce a run, but bit-for-bit equivalence across platforms
or hardware is not promised.

## Honest limitations

- Softmax outputs are **model scores**, not calibrated probabilities and not
  confidence guarantees.
- The classifier always answers with one of the 37 breeds. It **cannot reliably
  reject non-pet images**.
- A smoke model has seen 4 images per class for one epoch. Its predictions are
  close to random; that is expected and reported as such.
- The API binds to `127.0.0.1` only and has no authentication. It is a local
  development tool, not a deployment.

## Security and cost

- Uploads: 10 MB request-body cap enforced by the application (not just
  `Content-Length`), 50-megapixel decoded-image cap, decoded content validated
  (declared content type is ignored), only JPEG and PNG accepted, EXIF
  orientation applied, converted to RGB, never written to disk.
- Model loading: `torch.load(..., weights_only=True)` — no pickle execution;
  checksum, schema and preprocessing verified before use.
- No credentials, tokens or secrets anywhere in the product.
- Cost: USD 0. No cloud resources are created in G1.

## G3: Azure preparation and cost controls

No Azure resources yet. G3 adds read-only discovery, retail price evidence,
a versioned USD 100/60/40 budget policy, a lifetime cost ledger, session
admission with no bypass, an Azure Automation expiry watchdog (report-only
until live-verified) and two independent Terraform roots for the future AKS
lab. See [`docs/g3-cost-controls.md`](docs/g3-cost-controls.md) and
ADR 0012. Cloud cost of G3: USD 0.

## Not in G1 (deferred)

Docker, Kubernetes, Argo, Flux, Terraform, cloud storage, managed databases,
model registry, automated promotion, GPU training, HTMX, training from the UI.
