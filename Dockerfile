FROM python:3.11-slim

WORKDIR /opt/ouroboros-agent

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/ouroboros-agent/

ENV OUROBOROS_WORKSPACE=/var/lib/ouroboros/workspace_state
ENV OUROBOROS_SKILLS_DIR=/opt/ouroboros-agent/skills

EXPOSE 8787

CMD ["python3", "main.py", "--mode", "daemon", "--host", "0.0.0.0", "--port", "8787"]
