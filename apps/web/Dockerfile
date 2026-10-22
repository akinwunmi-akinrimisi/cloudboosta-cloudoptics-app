# Builds the static web app (build context: repository root). App Platform copies
# /src/apps/web/dist out of this image as the static site (infra/digitalocean/app.yaml).
FROM node:22.22.0-slim
ARG VITE_API_URL=/api
ARG VITE_API_MODE=http
ENV VITE_API_URL=$VITE_API_URL VITE_API_MODE=$VITE_API_MODE
RUN corepack enable && mkdir /src && chown node:node /src
USER node
WORKDIR /src
COPY --chown=node:node package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --chown=node:node apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @cloudoptics/web
COPY --chown=node:node infra/cloudformation infra/cloudformation
COPY --chown=node:node apps/web apps/web
RUN pnpm --filter @cloudoptics/web build
