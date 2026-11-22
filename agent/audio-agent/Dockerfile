# Demo 1 partner voice bridge ("Sofía"). Build for ARM64 Fargate from src/ as
# the context so the Starter_Kit package can be copied in alongside the bridge
# (task 3.7 moved the bridge onto a2a_connect for protocol, tracing and
# fragments). deploy.py builds it as:
#   finch build --platform linux/arm64 -f audio-agent/Dockerfile -t a2a-demo1-bridge:v1 <repo>/src
FROM public.ecr.aws/docker/library/python:3.15-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
WORKDIR /app

COPY audio-agent/bridge/requirements.txt /app/bridge/requirements.txt
RUN pip install --no-cache-dir -r /app/bridge/requirements.txt

# The Starter_Kit package the bridge imports (a2a_connect). Copied next to the
# bridge so `import a2a_connect` resolves without any PYTHONPATH wiring.
COPY starter-kit/a2a_connect /app/a2a_connect

# Runtime code only; the local harness, checks and tests stay out of the image.
COPY audio-agent/bridge/__init__.py audio-agent/bridge/_kitpath.py audio-agent/bridge/a2a.py \
     audio-agent/bridge/audio.py audio-agent/bridge/bearer.py audio-agent/bridge/config.py \
     audio-agent/bridge/nova.py audio-agent/bridge/secrets.py audio-agent/bridge/server.py \
     audio-agent/bridge/stubnova.py \
     audio-agent/bridge/tools.py audio-agent/bridge/tracing.py audio-agent/bridge/persona.txt \
     audio-agent/bridge/local_tools.py audio-agent/bridge/testpage.py audio-agent/bridge/persona_ko.txt \
     /app/bridge/
COPY audio-agent/bridge/engines /app/bridge/engines

RUN useradd --uid 10001 --no-create-home bridge
USER 10001

EXPOSE 8080
CMD ["python", "-m", "bridge.server"]
