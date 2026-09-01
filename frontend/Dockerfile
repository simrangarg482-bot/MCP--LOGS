# EKIP frontend production image (Phase 3 production-readiness pass).
#
# Build stage produces a static `dist/` bundle (`npm run build` ==
# `tsc --noEmit && vite build`, frontend/package.json); the runtime stage
# ships only that bundle behind a minimal static server -- no Node.js, no
# dev dependencies, no Vite dev server in the final image.
#
# `VITE_API_BASE_URL` is a BUILD ARG, not a runtime env var: Vite compiles
# every `import.meta.env.VITE_*` reference into the bundle at build time
# (frontend/src/api/config.ts), so it must be supplied to `docker build`
# (`--build-arg VITE_API_BASE_URL=https://api.example.com`), not to
# `docker run`. Railway makes every service variable available as a build arg
# automatically, so setting `VITE_API_BASE_URL` on the frontend service there
# is enough -- no extra wiring. This is also exactly why no secret may ever
# be a `VITE_*` value: anything compiled in is trivially readable in the
# shipped JavaScript by anyone who loads the page (see
# frontend/src/api/config.ts's own comment on this).

FROM node:22-slim AS build

WORKDIR /app

# Dependencies first (cached across builds whenever only source changes).
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_USE_MOCK_DATA=false
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# The platform (Railway, etc.) injects $PORT at runtime and routes to it;
# 8080 is only the fallback for a plain `docker run` with no PORT set.
ENV PORT=8080
# Restrict envsubst to exactly $PORT so nginx's own `$uri`/`$host` variables
# in the template are left intact (the nginx image would otherwise blank out
# every unset `$var` it finds).
ENV NGINX_ENVSUBST_FILTER="^PORT$"

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

# Deliberately NOT forcing a fully non-root USER here: nginx's own official
# image already ships a real, standard privilege-separation model out of the
# box (a root master process that only binds the listen port and manages workers;
# every worker process -- the one actually parsing client input -- already
# runs as the unprivileged `nginx` user by default, no config needed). A
# fully non-root setup would need rebinding to a port >=1024 and relocating
# nginx's pid/temp paths, changes this environment has no way to actually
# build-and-run to verify -- shipping an unverified, more fragile config
# over nginx's own tested default would trade a real, working baseline for
# an unconfirmed one. Revisit with a real container runtime available to
# test against.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider "http://127.0.0.1:${PORT}/" || exit 1
