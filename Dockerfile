FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ipmitool openssh-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

VOLUME ["/data"]
EXPOSE 8081

CMD ["python", "app.py"]
