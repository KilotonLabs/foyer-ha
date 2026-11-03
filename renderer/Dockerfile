ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache nodejs npm chromium font-noto font-noto-emoji ttf-dejavu
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY server.mjs ./
# with-contenv: s6 /init strips the container env otherwise, and SUPERVISOR_TOKEN is needed for discovery.
CMD ["/command/with-contenv", "node", "server.mjs"]
