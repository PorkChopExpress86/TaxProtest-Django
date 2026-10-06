"""Benchmark Harris candidate staging throughput and memory consumption."""

from counties.common.benchmarking import BenchmarkCommand
from counties.harris.benchmark import benchmark_harris


class Command(BenchmarkCommand):
    county = "harris"
    help = "Benchmark Harris ETL candidate staging throughput and memory consumption."

    def benchmark(self, rows):
        return benchmark_harris(rows)
