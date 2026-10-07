"""Benchmark Brazos candidate staging throughput and memory consumption."""

from counties.brazos.benchmark import benchmark_brazos
from counties.common.benchmarking import BenchmarkCommand


class Command(BenchmarkCommand):
    county = "brazos"
    help = "Benchmark Brazos ETL candidate staging throughput and memory consumption."

    def benchmark(self, rows):
        return benchmark_brazos(rows)
