
# Stage 1: Build the React/Vite application
FROM node:20-slim AS build-stage

WORKDIR /app

# Copy package files
COPY Frontend/package.json Frontend/package-lock.json ./

# Install dependencies
RUN npm install --include=dev

# Copy the rest of the application source
COPY Frontend/ .

# Build the application
# Use VITE_API_BASE=http://localhost:4001 as default, can be overridden at build time
ARG VITE_API_BASE=http://13.232.27.174
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

# Stage 2: Serve the application with Nginx
FROM nginx:stable-alpine

# Copy the build output to Nginx's html directory
COPY --from=build-stage /app/dist /usr/share/nginx/html

# Copy custom nginx config for SPA routing (optional but recommended)
COPY Frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
