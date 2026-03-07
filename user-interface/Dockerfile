# Multi-stage: build Angular app, then serve with nginx.
# Production env uses relative URLs (/api/...) so nginx can proxy to the backend.
# Build from repo root: docker build -f user-interface/Dockerfile user-interface
# Or from user-interface: docker build -t strands-ui .

# Stage 1: build Angular
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json* ./
# Use npm install so build works when lock file is out of sync with package.json (e.g. after adding vitest/jsdom).
# For reproducible installs, run "npm install" locally and commit the updated package-lock.json, then switch back to "npm ci".
RUN npm install

COPY . .
RUN npm run build

# Stage 2: nginx serve static + proxy API
FROM nginx:alpine
COPY --from=build /app/dist/user-interface/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
