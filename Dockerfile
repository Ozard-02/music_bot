FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    flac \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# pydoll is an import-time dependency of SpotiFLAC (amazon -> signed_session_mono),
# but never invoked: no chromium binary is installed, so no browser can spawn.
RUN pip install --no-cache-dir SpotiFLAC==1.6.0 pydoll-python

COPY . .

CMD ["python", "bot.py"]
