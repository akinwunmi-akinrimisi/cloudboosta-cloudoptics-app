# Builds the public site (build context: repository root). App Platform copies
# /src/apps/site/dist out of this image as the static site served at / (infra/digitalocean/
# app.yaml, SITE-13), including /cfn/* (the CloudFormation templates) and the 404 page that
# forwards old app paths to /app/...
FROM node:22.22.0-slim
ARG SITE_URL=https://cloudoptics.cloudboosta.com
ENV SITE_URL=$SITE_URL
RUN corepack enable && mkdir /src && chown node:node /src
USER node
WORKDIR /src
COPY --chown=node:node package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --chown=node:node apps/site/package.json apps/site/package.json
COPY --chown=node:node packages/design-tokens/package.json packages/design-tokens/package.json
RUN pnpm install --frozen-lockfile --filter @cloudoptics/site
COPY --chown=node:node infra/cloudformation infra/cloudformation
COPY --chown=node:node packages/design-tokens packages/design-tokens
COPY --chown=node:node apps/site apps/site
RUN pnpm --filter @cloudoptics/site build
