# Mirror packages

## Docker Images
- https://hub.docker.com/_/busybox (tags: 1.37.0, 1.31.1)
- https://hub.docker.com/_/alpine (tags: 3.23.3)
- https://hub.docker.com/_/nginx (tags: 1.25, 1.25-alpine, 1.26, 1.26-alpine, 1.27, 1.27-alpine)
- https://hub.docker.com/_/postgres (tags: 17, 18.3)
- https://hub.docker.com/_/redis → `official-redis` (tags: 7.2, 7.4, 7.6)
- https://hub.docker.com/r/alpine/kubectl (tags: 1.34.2)
- https://hub.docker.com/r/amazon/aws-cli (tags: 2.33.7)
- https://hub.docker.com/r/bitnami/postgresql → `bitnami-postgresql` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/postgresql → `bitnamisecure-postgresql` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/redis → `bitnamisecure-redis` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/redis → `redis` (tags: latest) *(legacy)*
- https://hub.docker.com/r/grafana/grafana (tags: 11.4.0, 12.3.1)
- https://hub.docker.com/r/grafana/grafana-image-renderer (tags: latest)
- https://hub.docker.com/r/nginxdemos/hello → `nginxdemos-hello` (tags: 0.4)
- https://hub.docker.com/r/omnistrate/noop → `noop` (tags: latest)
- https://hub.docker.com/r/redis/redis-stack-server → `redis-stack-server` (tags: 6.2.6-v7)
- https://hub.docker.com/r/tomnislav/pg-with-exporter → `pg-with-exporter` (tags: 2.0)

## Helm Charts
All at `oci://ghcr.io/omnistrate/charts/`:
- `redis` 20.0.0, 20.1.0, 20.2.0, 22.0.7 from `oci://registry-1.docker.io/bitnamicharts`
- `postgresql` 16.7.27 from `oci://registry-1.docker.io/bitnamicharts`
- `nginx` 18.0.0, 18.1.0 from `oci://registry-1.docker.io/bitnamicharts`
- `postgres` 0.18.3 from `oci://registry-1.docker.io/cloudpirates`
- `common` 2.2.0 from `oci://registry-1.docker.io/cloudpirates`

Created to avoid throttling from DockerHub