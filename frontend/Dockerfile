# FireProtect dashboard — build with Vite, serve with nginx.

FROM node:20-alpine AS build

WORKDIR /app

# Dependency layer first: source edits should not re-run npm ci.
COPY package.json package-lock.json* ./
RUN npm ci 2>/dev/null || npm install --no-audit --no-fund

COPY . .

# `npm run build` runs `tsc --noEmit && vite build`, so a type error fails the
# image build rather than shipping broken JavaScript.
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD wget -q --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
