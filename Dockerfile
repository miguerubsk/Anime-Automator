FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends mkvtoolnix ca-certificates tzdata openssh-client && \
    rm -rf /var/lib/apt/lists/*

# rclone: descargas de la unidad de Drive de Japan-Paw (binario estático)
COPY --from=rclone/rclone:1.76.0 /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY automator/ ./automator/

RUN mkdir -p /data/tmp

EXPOSE 5000

CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
