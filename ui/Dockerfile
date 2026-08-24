# Portable Doc1 UI image. Installation policy is mounted at runtime, so the image bytes
# remain identical across institutions.
FROM node:22.22.0-alpine@sha256:e4bf2a82ad0a4037d28035ae71529873c069b13eb0455466ae0bc13363826e34 AS builder

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# test:unit, not test: the build context is ui/, so the repo-scope suite (`npm run test:repo`,
# which checks that the embed host fixtures under tests/ pin the published loader SRI) has no
# files to read here. Widening the context to the repo root would put Python sources and test
# fixtures into a UI image that must stay tenant-neutral and small, so that suite runs in the
# CI jobs that have the full checkout instead.
RUN npm run lint \
 && npm run test:unit \
 && npm run build

FROM node:22.22.0-alpine@sha256:e4bf2a82ad0a4037d28035ae71529873c069b13eb0455466ae0bc13363826e34 AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

# A digest pin freezes the base image, and therefore also freezes its unpatched packages.
# Reproducible and patched are independent properties; the pin only buys the first.
RUN apk upgrade --no-cache

# Remove npm (and its bundled corepack/yarn) from the RUNTIME image. Next.js standalone
# output ships its own minimal node_modules and is started with `node server.js`, so nothing
# here installs packages — but the base image carries a full npm, and npm BUNDLES its
# dependencies. Every Node advisory in the 2026-08-24 promotion scan came from that bundle
# (tar, sigstore, glob, minimatch, brace-expansion, picomatch, ip-address); not one was a
# dependency of this application, and none appeared in package-lock.json, so no npm upgrade
# in the builder could have reached them. Same shape as pip vendoring msgpack in the API
# image: the scanner is right, the finding is real, and the package manager is the thing that
# does not belong in a serving container.
RUN rm -rf /usr/local/lib/node_modules/npm \
           /usr/local/lib/node_modules/corepack \
           /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
           /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg

RUN addgroup -S doc1 -g 10001 \
 && adduser -S doc1 -u 10001 -G doc1

COPY --from=builder --chown=doc1:doc1 /app/.next/standalone ./
COPY --from=builder --chown=doc1:doc1 /app/.next/static ./.next/static
COPY --from=builder --chown=doc1:doc1 /app/public ./public

USER doc1
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/agent/ready >/dev/null || exit 1

CMD ["node", "server.js"]
