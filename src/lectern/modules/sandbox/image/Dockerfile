# The image scripts run in: Python plus a few libraries for tables, dates and spreadsheets.
# The base image (by digest, packaging/pins.sh) and the libraries are pinned, so a script
# behaves the same on every machine and every rebuild. Changing either needs a new IMAGE tag
# in container.py, or installs that already built the old image keep using it.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

RUN pip install --no-cache-dir \
        numpy==2.2.6 \
        pandas==2.2.3 \
        python-dateutil==2.9.0.post0 \
        openpyxl==3.1.5

WORKDIR /job
