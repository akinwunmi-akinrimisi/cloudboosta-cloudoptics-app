# Builds the static web app (build context: repository root). App Platform copies
# /src/apps/web/dist out of this image as the static site (infra/digitalocean/app.yaml).
FROM node:22.22.0-slim
ARG VITE_API_URL=/api
ARG VITE_API_MODE=http
ENV VITE_API_URL=$VITE_API_URL VITE_API_MODE=$VITE_API_MODE
WORKDIR /src
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @cloudoptics/web
COPY infra/cloudformation infra/cloudformation
COPY apps/web apps/web
RUN pnpm --filter @cloudoptics/web build
