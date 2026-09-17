# syntax=docker/dockerfile:1.7
# CareOS operator console (Next.js standalone output). Build context: repository root.
#
#   docker build -f apps/web/Dockerfile -t careos-web .

ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1
# Dependency layer (cached until a manifest or the lockfile changes)
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/contracts/package.json packages/contracts/package.json
RUN npm ci --no-audit --no-fund
COPY packages/contracts ./packages/contracts
COPY apps/web ./apps/web
RUN npm run build -w @careos/web

FROM node:${NODE_VERSION}-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup -S -g 10001 careos && adduser -S -u 10001 -G careos careos
COPY --from=build --chown=careos:careos /repo/apps/web/.next/standalone ./
COPY --from=build --chown=careos:careos /repo/apps/web/.next/static ./apps/web/.next/static
USER careos
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
