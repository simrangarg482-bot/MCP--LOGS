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
# `docker run`. This is also exactly why no secret may ever be a `VITE_*`
# value: anything compiled in is trivially readable in the shipped
# JavaScript by anyone who loads the page (see frontend/src/api/config.ts's
# own comment on this).

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

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# Deliberately NOT forcing a fully non-root USER here: nginx's own official
# image already ships a real, standard privilege-separation model out of the
# box (a root master process that only binds port 80 and manages workers;
# every worker process -- the one actually parsing client input -- already
# runs as the unprivileged `nginx` user by default, no config needed). A
# fully non-root setup would need rebinding to a port >=1024 and relocating
# nginx's pid/temp paths, changes this environment has no way to actually
# build-and-run to verify -- shipping an unverified, more fragile config
# over nginx's own tested default would trade a real, working baseline for
# an unconfirmed one. Revisit with a real container runtime available to
# test against.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:80/ || exit 1
