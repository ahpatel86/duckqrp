# Container build of qrp-duckdb.
#
#   docker build -t qrp-duckdb .
#   docker run --rm -v /path/to/scdm:/data:ro -v /path/to/out:/out \
#       -v /path/to/study.json:/study.json:ro \
#       qrp-duckdb run --study /study.json --indata /data --out /out
#
# Mount the SCDM read-only: the pipeline never writes to its input, and
# a read-only mount makes that a guarantee rather than a property of
# the code.
#
# NOT TESTED in the environment this was written in — no container
# runtime was available. The executable route
# (tools/build_executable.sh) is the one that has been verified.
FROM python:3.12-slim

WORKDIR /opt/qrp
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && useradd --create-home --uid 1000 qrp

# Run as a non-root user: nothing here needs root, and a container
# holding patient data should not have it.
USER qrp
ENTRYPOINT ["qrp"]
CMD ["--help"]
