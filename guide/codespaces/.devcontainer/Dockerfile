FROM node:24-bookworm-slim AS node
FROM mcr.microsoft.com/devcontainers/python:1.1.4-3.9-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global @openai/codex@0.166.0 \
    && npm cache clean --force
