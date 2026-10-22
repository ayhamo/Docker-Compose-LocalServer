FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# tzdata so schedules run on local (Berlin) time
RUN apk add --no-cache tzdata

WORKDIR /app
COPY requirements.txt .
# Install deps, then remove pip itself (not needed at runtime)
RUN pip install -r requirements.txt \
 && pip uninstall -y pip \
 && rm -rf /root/.cache /usr/local/lib/python3.13/ensurepip

COPY plug_server.py index.html ./

USER 1000:1000
EXPOSE 8080
CMD ["python", "plug_server.py"]
