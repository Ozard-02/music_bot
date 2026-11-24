FROM python:3.14-slim

# Build identity, passed by CI so the startup log shows which commit is
# actually running (makes "is the fix deployed?" obvious).
ARG BUILD_COMMIT=dev
ENV BUILD_COMMIT=${BUILD_COMMIT}

# Cap glibc malloc arenas: threaded Python (to_thread + subprocesses) otherwise
# spawns an arena per thread and RSS balloons. 2 is plenty for our workload.
ENV MALLOC_ARENA_MAX=2

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    flac \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# pydoll is an import-time dependency of SpotiFLAC (amazon -> signed_session_mono),
# but never invoked: no chromium binary is installed, so no browser can spawn.
# Node.js (installed above) is required by the JS extension runtime that
# SpotiFLAC 5.x uses for every download provider.
RUN pip install --no-cache-dir SpotiFLAC==5.0.6 pydoll-python==3.0.1

COPY . .

CMD ["python", "bot.py"]