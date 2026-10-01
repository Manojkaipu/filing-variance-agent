import pytest

from src.utils.spark_session import get_spark


@pytest.fixture(scope="session")
def spark():
    spark = get_spark("filing-variance-agent-tests", master="local[2]", shuffle_partitions=2)
    spark.sparkContext.setLogLevel("ERROR")
    yield spark
    spark.stop()


@pytest.fixture(scope="session")
def spark_writes(spark, tmp_path_factory):
    """Skips tests that write Parquet when this machine can't. On Windows,
    Spark file writes need Hadoop's winutils.exe and hadoop.dll in
    %HADOOP_HOME%\\bin; run these tests on Linux/macOS/WSL or in CI instead."""
    probe = tmp_path_factory.mktemp("write_probe") / "probe"
    try:
        spark.range(1).write.parquet(str(probe))
    except Exception as e:  # Py4JJavaError wrapping UnsatisfiedLinkError, FileNotFoundException, ...
        pytest.skip(f"Spark cannot write files on this machine ({type(e).__name__}); see tests/conftest.py")
    return True
