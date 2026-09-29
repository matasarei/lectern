# The image scripts run in: Python plus a few libraries for tables, dates and spreadsheets.
# Versions are pinned so a script behaves the same on every machine and every rebuild.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        numpy==2.2.6 \
        pandas==2.2.3 \
        python-dateutil==2.9.0.post0 \
        openpyxl==3.1.5

WORKDIR /job
