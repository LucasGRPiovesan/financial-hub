# syntax=docker/dockerfile:1
FROM node:24-alpine AS base
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /app
RUN chown node:node /app
USER node

# --- deps: locked install, shared by dev and build ---
FROM base AS deps
COPY --chown=node:node package.json package-lock.json ./
RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 npm ci --no-audit --no-fund

# --- dev: Vite dev server, source bind-mounted by compose ---
FROM deps AS dev
COPY --chown=node:node . .
EXPOSE 5173
CMD ["npm", "run", "dev"]

# --- build: production bundle ---
FROM deps AS build
COPY --chown=node:node . .
RUN npm run build

# --- prod: static files behind unprivileged nginx ---
FROM nginxinc/nginx-unprivileged:alpine AS prod
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
