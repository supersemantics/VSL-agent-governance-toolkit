import pytest
from vsl_core.conformance.suite import run_conformance_suite

from vsl_agt import AGTAdapter


def test_conformance_suite_passes():
    failures = run_conformance_suite(AGTAdapter())
    assert failures == [], "\n".join(failures)


def test_identity_key_required():
    with pytest.raises(ValueError):
        AGTAdapter(identity_key="")
