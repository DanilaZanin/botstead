from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_internal_gateway_url_is_documented_as_core_only():
    gateway_docs = (ROOT / 'docs/gateway.md').read_text()
    contracts = (ROOT / 'docs/contracts.md').read_text()
    env_example = (ROOT / 'deploy/.env.example').read_text()

    assert 'BOTHUB_INTERNAL_URL' in gateway_docs
    assert 'http://core:8080' in gateway_docs
    assert 'not publicly exposed' in gateway_docs.lower()
    assert 'BOTHUB_INTERNAL_URL=http://core:8080' in env_example
    assert 'BOTHUB_INTERNAL_URL' in contracts
    assert 'http://core:8080' in contracts


def test_external_gateway_examples_are_labeled_separate_deployments():
    gateway_docs = (ROOT / 'docs/gateway.md').read_text()

    assert 'separate gateway deployment' in gateway_docs.lower()
