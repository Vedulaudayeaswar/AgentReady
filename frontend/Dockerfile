FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/security-headers.conf nginx/common.conf nginx/http-maps.conf /etc/nginx/snippets/
COPY nginx.conf /etc/nginx/conf.d/default.conf
# production server template (rendered by the nginx entrypoint when mounted into /etc/nginx/templates)
COPY nginx/prod.conf /etc/nginx/prod.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443
