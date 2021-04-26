"""
Simple benchmarking exercise

"""
import pathlib

import anvil.scripts.anvil_benchmark as benchmark


TEST_SAMPLE_CONFIG = pathlib.Path(__file__).with_name("benchmark_sample_config.yml")


def test_benchmark_runs():
    """Test benchmark runs, in future test numbers."""
    benchmark.main(_sample_runcard_path=TEST_SAMPLE_CONFIG)
