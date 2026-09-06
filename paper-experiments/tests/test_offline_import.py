import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("initial_value", [None, "False"])
def test_paper_adapter_import_does_not_fetch_provider_metadata(tmp_path, initial_value):
    environment = os.environ.copy()
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    if initial_value is not None:
        environment["LITELLM_LOCAL_MODEL_COST_MAP"] = initial_value
    environment["MSWEA_GLOBAL_CONFIG_DIR"] = str(tmp_path)
    environment["MSWEA_SILENT_STARTUP"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import httpx

requests = []

def reject_network(self, request, *args, **kwargs):
    requests.append(str(request.url))
    raise RuntimeError("Network is unavailable in this import test")

httpx.Client.send = reject_network
import paper_experiments.mini_adapter
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

assert not requests, requests
assert get_model_cost_map_source_info()["is_env_forced"]
""",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
