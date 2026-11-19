# Builds the static web app (build context: repository root). App Platform copies
# /src/apps/web/dist out of this image as a static site (infra/digitalocean/app.yaml): the
# app at /app (VITE_BASE=/app/) and the demo at /demo (VITE_API_MODE=demo VITE_BASE=/demo/).
FROM node:22.23.5-slim
ARG VITE_API_URL=/api
ARG VITE_API_MODE=http
ARG VITE_BASE=/
ENV VITE_API_URL=$VITE_API_URL VITE_API_MODE=$VITE_API_MODE VITE_BASE=$VITE_BASE
RUN corepack enable && mkdir /src && chown node:node /src
USER node
WORKDIR /src
COPY --chown=node:node package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --chown=node:node apps/web/package.json apps/web/package.json
COPY --chown=node:node packages/design-tokens/package.json packages/design-tokens/package.json
RUN pnpm install --frozen-lockfile --filter @cloudoptics/web
COPY --chown=node:node infra/cloudformation infra/cloudformation
COPY --chown=node:node packages/design-tokens packages/design-tokens
COPY --chown=node:node apps/web apps/web
RUN pnpm --filter @cloudoptics/web build
