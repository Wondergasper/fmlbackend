import os
import pytest

# Ignore known non-Python artifacts in the test folder
collect_ignore = ["test_results.txt"]


def pytest_collection_modifyitems(config, items):
    """Skip integration-marked tests by default unless RUN_INTEGRATION=1."""
    run_integration = os.getenv("RUN_INTEGRATION", "0") == "1"
    if run_integration:
        return
    skip_marker = pytest.mark.skip(reason="Integration test (set RUN_INTEGRATION=1 to run)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)
