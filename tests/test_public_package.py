from importlib.metadata import metadata

import short_term_memory


def test_distribution_and_import_package_have_standalone_identity() -> None:
    assert metadata("short-term-memory")["Name"] == "short-term-memory"
    assert short_term_memory.__version__ == "0.1.0"
