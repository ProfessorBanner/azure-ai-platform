"""Deterministic tokenisation.

One rule, applied everywhere: lowercase, then split on runs of non-alphanumeric
characters, then drop empties. Nothing else. No stemming, no stop-word list, no
locale awareness, no external dependency.

The restraint is deliberate. Stemming is a second source of behaviour that has
to be pinned, explained and version-controlled, and on a 177-chunk corpus of
technical prose it would mostly conflate terms that this domain distinguishes.
`str.lower()` is used rather than `casefold()` because casefold applies
locale-independent but language-specific mappings (German ß, for instance) that
would change tokenisation of text this corpus does not contain — an unnecessary
variable.

HOW PLATFORM IDENTIFIERS ARE TREATED
------------------------------------
Splitting on non-alphanumeric boundaries means compound identifiers become
several tokens. This is the intended behaviour, and it is worth being explicit
about because it decides what a query can match:

  gpt-4-1-mini          -> ["gpt", "4", "1", "mini"]
  dev/stg/prod          -> ["dev", "stg", "prod"]
  /openai/v1/           -> ["openai", "v1"]
  azurerm_cognitive_account
                        -> ["azurerm", "cognitive", "account"]
  rg-aiplatform-sandbox -> ["rg", "aiplatform", "sandbox"]
  platform/dev.tfstate  -> ["platform", "dev", "tfstate"]
  Microsoft.CognitiveServices
                        -> ["microsoft", "cognitiveservices"]

The consequence that matters: asking about "dev" matches a chunk mentioning
`dev/stg/prod`, and asking about "gpt-4-1-mini" matches a chunk mentioning
`gpt-4.1-mini`, because both reduce to the same token sequence. That is usually
what a reader wants from documentation search.

The cost is equally real: the components are indexed separately, so a query for
`terraform state` cannot distinguish `platform/dev.tfstate` from a chunk that
merely mentions Terraform and state in the same paragraph. Phrase and adjacency
matching are not part of BM25 and are not attempted here.
"""

from __future__ import annotations

import re

# Runs of anything that is not an ASCII letter or digit are separators. Written
# as an explicit ASCII class rather than \\W so behaviour cannot vary with
# Unicode flags or locale.
_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase alphanumeric tokens, in source order.

    Order is preserved because term frequency is counted from the result;
    duplicates are meaningful and must not be collapsed.
    """
    return [token for token in _SEPARATOR_PATTERN.split(text.lower()) if token]
