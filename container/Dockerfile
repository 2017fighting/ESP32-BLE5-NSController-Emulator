# The container image: one process, four seams, four screens (§8.1).
#
# Stage 1 builds the UI with vite (§8.1's asset pipeline: a build served by
# aiohttp). Stage 2 is `python:3.12-slim`, as §8.1 fixes — the UI source never
# reaches the runtime stage, only its `dist/`.
#
# Build from the repository root (compose does this via `context: ..`):
#   docker compose -f container/compose.yaml up --build

# syntax=docker/dockerfile:1

FROM node:22-slim AS ui
WORKDIR /ui
# Dependencies first, so a source edit does not re-resolve the tree.
COPY container/web/package.json container/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY container/web/ ./
# `tsc -b && vite build`; a type error fails the image, which is the point.
RUN npm run build


FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY container/requirements.txt ./container/requirements.txt
RUN pip install --no-cache-dir -r container/requirements.txt
COPY container/ ./container/
COPY --from=ui /ui/dist ./container/web/dist
EXPOSE 8080
# The three mounts (§10.4) and the device are deployment, not image, concerns.
CMD ["python", "-m", "container.ns2container"]
