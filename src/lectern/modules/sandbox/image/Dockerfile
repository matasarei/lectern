# The image scripts run in: Python plus a few libraries for tables, dates and spreadsheets.
# The base image (by digest, packaging/pins.sh) and the libraries are pinned, so a script
# behaves the same on every machine and every rebuild. Changing either needs a new IMAGE tag
# in container.py, or installs that already built the old image keep using it.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

RUN pip install --no-cache-dir \
        numpy==2.2.6 \
        pandas==2.2.3 \
        python-dateutil==2.9.0.post0 \
        openpyxl==3.1.5

WORKDIR /job
