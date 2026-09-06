"""Reproducible controllers and analysis for the paper's registered experiments."""

import os

# Provider charges control the ledger; importing an SDK must not fetch other websites.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
