FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Debian security fixes newer than the base image.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# The base image's setuptools and wheel are build tools the service never imports; removing
# them keeps their vendored copies (and their advisories) out of the running image.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall --yes setuptools wheel
COPY app ./app
EXPOSE 8020
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
